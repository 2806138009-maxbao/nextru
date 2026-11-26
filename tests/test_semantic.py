"""Semantic recall: chunking, rank fusion, the embedding service and search fallback."""

from __future__ import annotations

import os
import socket
import threading

from helpers import NexusTest

from nexus import search as search_index
from nexus import semantic
from nexus.agent_tools import AgentTools
from nexus.errors import Status
from nexus.ingest import FetchedDocument, ingest_document
from nexus.semantic_service import HashEmbedder, SemanticIndex, VectorStore, make_server
from nexus.service import dispatch

SCOPE = "research:sem"
OTHER = "research:sem-other"

DOC = """Studio notes｜lights and sound
One｜Order of purchase
Buy the lights first, the microphone comes after them.
Two｜Layout
""" + "\n".join(f"Paragraph {i} about framing and colour grading." for i in range(30)) + "\n"


def make(text: str, doc_id: str) -> FetchedDocument:
    return FetchedDocument("GOOGLE_DRIVE", doc_id, doc_id, "1", None, None, text)


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        self.encoded = 0

    def encode(self, texts):
        self.encoded += len(texts)
        return super().encode(texts)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SemanticTests(NexusTest):

    def setUp(self) -> None:
        super().setUp()
        ingest_document(self.store, make(DOC, "SEMDOC1"), SCOPE, object_id="doc/studio")
        ingest_document(self.store, make("Other scope｜notes\nlights only here.\n", "SEMDOC2"), OTHER,
                        object_id="doc/other")
        search_index.ensure_index(self.conn)
        self.conn.commit()
        self._env = os.environ.pop("NEXUS_SEMANTIC_URL", None)

    def tearDown(self) -> None:
        if getattr(self, "server", None):
            self.server.shutdown()
            self.server.server_close()
            self.index.store.conn.close()
        os.environ.pop("NEXUS_SEMANTIC_URL", None)
        if self._env:
            os.environ["NEXUS_SEMANTIC_URL"] = self._env
        super().tearDown()

    def start_service(self) -> SemanticIndex:
        self.emb = CountingEmbedder()
        store = VectorStore(os.path.join(self._tmp_name, "vectors.sqlite"), self.emb.name, self.emb.dim)
        self.index = SemanticIndex(self.db_path, store, self.emb)
        self.index.refresh()
        port = free_port()
        self.server = make_server(self.index, "127.0.0.1", port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["NEXUS_SEMANTIC_URL"] = f"http://127.0.0.1:{port}"
        return self.index

    def q(self, query: str, scope: str = SCOPE, **extra) -> dict:
        return dispatch(self.core, "search", {"query": query, "scope": scope, **extra})

    # -------------------------------------------------------------- chunking
    def test_chunks_join_consecutive_paragraphs_and_carry_scope(self) -> None:
        chunks = semantic.build_chunks(self.conn)
        doc = [c for c in chunks if c["object_id"] == "doc/studio" and c["kind"] == "paragraph"]
        self.assertGreater(len(doc), 1)
        self.assertLess(len(doc), 30)                       # 30+ paragraphs -> a few windows
        name = self.store.get_object("doc/studio")["display_name"]
        self.assertTrue(all(c["text"].startswith(name + "\n") for c in doc))  # title prefix
        self.assertTrue(all(c["scope"] == SCOPE for c in doc))
        self.assertTrue(all(c["item_id"] == c["item_ids"][0] for c in doc))
        self.assertEqual(len({c["key"] for c in chunks}), len(chunks))
        self.assertEqual([c["key"] for c in chunks], [c["key"] for c in semantic.build_chunks(self.conn)])

    # -------------------------------------------------------------- fusion
    def test_merge_boosts_shared_hits_and_adds_semantic_only(self) -> None:
        text = [{"item_id": "a", "object_id": "o1", "kind": "paragraph", "score": 9},
                {"item_id": "b", "object_id": "o1", "kind": "paragraph", "score": 5}]
        sem = [{"item_id": "c", "item_ids": ["c", "b"], "object_id": "o1", "kind": "paragraph",
                "similarity": 0.8, "snippet": "x"},
               {"item_id": "z", "item_ids": ["z"], "object_id": "o2", "kind": "paragraph",
                "similarity": 0.7, "snippet": "y"}]
        out = semantic.merge(text, sem)
        self.assertEqual([h["item_id"] for h in out][:2], ["b", "a"])   # b: text + semantic
        self.assertEqual(out[0]["match"], "both")
        self.assertEqual(out[0]["text_score"], 5)
        z = next(h for h in out if h["item_id"] == "z")
        self.assertEqual(z["match"], "semantic")
        self.assertEqual(z["matched_terms"], [])
        self.assertNotIn("c", [h["item_id"] for h in out])              # folded into b

    # -------------------------------------------------------------- service
    def test_refresh_embeds_only_new_text(self) -> None:
        idx = self.start_service()
        first = self.emb.encoded
        self.assertEqual(idx.pending, 0)
        self.assertFalse(idx.refresh())                      # nothing changed
        ingest_document(self.store, make("New｜doc\nA single new paragraph.\n", "SEMDOC3"), SCOPE,
                        object_id="doc/new")
        search_index.ensure_index(self.conn)
        self.conn.commit()
        self.assertTrue(idx.refresh())
        self.assertLessEqual(self.emb.encoded - first, 3)    # the new doc's chunks only
        self.assertEqual(len(idx.ready), len(idx.chunks))

    def test_semantic_hit_where_no_query_word_appears(self) -> None:
        self.start_service()
        env = self.q("lighting")                            # the text only says "lights"
        self.assertStatus(env, Status.OK)
        self.assertEqual(env["result"]["semantic"], "ok")
        top = env["result"]["hits"][0]
        self.assertEqual(top["object_id"], "doc/studio")
        self.assertEqual(top["match"], "semantic")
        self.assertTrue(top["anchor"])
        self.assertNotIn("doc/other", {h["object_id"] for h in env["result"]["hits"]})

    def test_agent_search_marks_semantic_hits(self) -> None:
        self.start_service()
        out = AgentTools(self.core, SCOPE).nexus_search("lighting")
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["hits"][0]["match"], "semantic")
        self.assertEqual(out["documents"][0]["object_id"], "doc/studio")

    def test_service_down_falls_back_to_text(self) -> None:
        os.environ["NEXUS_SEMANTIC_URL"] = f"http://127.0.0.1:{free_port()}"
        env = self.q("lights")
        self.assertStatus(env, Status.OK)
        self.assertEqual(env["result"]["semantic"], "unavailable")
        self.assertEqual(env["result"]["hits"][0]["object_id"], "doc/studio")
        out = AgentTools(self.core, SCOPE).nexus_search("lights")
        self.assertIn("unavailable", out["semantic"])

    def test_off_by_default(self) -> None:
        env = self.q("lights")
        self.assertNotIn("semantic", env["result"])
        self.assertNotIn("match", env["result"]["hits"][0])
