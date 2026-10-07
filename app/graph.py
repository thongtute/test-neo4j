"""Lớp làm việc với Neo4j.

Mô hình đồ thị:
    (:Video)-[:HAS_SEGMENT {order}]->(:Segment)
    (:Segment)-[:NEXT]->(:Segment)            # thứ tự các đoạn trong cùng video
    (:Segment)-[:SIMILAR_TO {distance}]-(:Segment)  # 2 đoạn có hình ảnh gần giống nhau
"""
from __future__ import annotations

import logging
import os
import time

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

from splitter import hamming

log = logging.getLogger(__name__)

SCHEMA = [
    "CREATE CONSTRAINT video_id IF NOT EXISTS FOR (v:Video) REQUIRE v.id IS UNIQUE",
    "CREATE CONSTRAINT segment_id IF NOT EXISTS FOR (s:Segment) REQUIRE s.id IS UNIQUE",
    "CREATE INDEX segment_video IF NOT EXISTS FOR (s:Segment) ON (s.video_id)",
    "CREATE INDEX segment_dhash IF NOT EXISTS FOR (s:Segment) ON (s.dhash)",
    "CREATE INDEX video_filename IF NOT EXISTS FOR (v:Video) ON (v.filename)",
]


class Graph:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.uri = uri
        self.database = database
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    @classmethod
    def from_env(cls) -> "Graph":
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "password123"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )

    def close(self) -> None:
        self.driver.close()

    def _q(self, query: str, **params):
        records, _, _ = self.driver.execute_query(
            query, parameters_=params, database_=self.database
        )
        return [r.data() for r in records]

    # ---------- khởi động ----------
    def wait_until_ready(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                self.driver.verify_connectivity()
                log.info("Đã kết nối Neo4j tại %s", self.uri)
                return
            except AuthError:
                raise
            except (ServiceUnavailable, OSError) as e:
                if time.time() > deadline:
                    raise RuntimeError(f"Không kết nối được Neo4j tại {self.uri}: {e}") from e
                log.info("Đang chờ Neo4j khởi động...")
                time.sleep(3)

    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self._q(stmt)

    def mark_interrupted(self) -> None:
        """Video đang xử lý dở khi app bị tắt -> đánh dấu lỗi để người dùng bấm 'Cắt lại'."""
        self._q(
            "MATCH (v:Video) WHERE v.status IN ['queued','processing'] "
            "SET v.status = 'error', v.error = 'Bị gián đoạn do app khởi động lại. Hãy bấm Cắt lại.'"
        )

    # ---------- ghi ----------
    def create_video(self, props: dict) -> None:
        self._q("MERGE (v:Video {id: $id}) SET v += $props", id=props["id"], props=props)

    def update_video(self, video_id: str, props: dict) -> None:
        self._q("MATCH (v:Video {id: $id}) SET v += $props", id=video_id, props=props)

    def add_segments(self, video_id: str, segments: list[dict]) -> None:
        """Tạo node Segment, nối với Video và nối các đoạn liên tiếp bằng NEXT."""
        self._q(
            """
            MATCH (v:Video {id: $vid})
            UNWIND $segs AS s
            MERGE (seg:Segment {id: s.id})
            SET seg += s
            MERGE (v)-[r:HAS_SEGMENT]->(seg)
            SET r.order = s.index
            """,
            vid=video_id, segs=segments,
        )
        self._q(
            """
            MATCH (:Video {id: $vid})-[:HAS_SEGMENT]->(s:Segment)
            WITH s ORDER BY s.index
            WITH collect(s) AS segs
            UNWIND range(0, size(segs) - 2) AS i
            WITH segs[i] AS a, segs[i + 1] AS b
            MERGE (a)-[:NEXT]->(b)
            """,
            vid=video_id,
        )

    def delete_segments(self, video_id: str) -> None:
        self._q(
            """
            MATCH (:Video {id: $vid})-[:HAS_SEGMENT]->(s:Segment)
            DETACH DELETE s
            """,
            vid=video_id,
        )

    def link_similar(self, video_id: str, max_distance: int) -> int:
        """So dấu vân tay ảnh (dHash) của các đoạn mới với mọi đoạn đã có (kể cả video khác).

        Hai đoạn có khoảng cách Hamming <= max_distance được nối bằng SIMILAR_TO.
        Trả về số cặp đã nối.
        """
        if max_distance < 0:
            return 0
        rows = self._q(
            """
            MATCH (s:Segment)
            WHERE s.dhash IS NOT NULL AND coalesce(s.flat, false) = false
            RETURN s.id AS id, s.video_id AS video_id, s.index AS idx, s.dhash AS h
            """
        )
        new = [r for r in rows if r["video_id"] == video_id]
        pairs = []
        for n in new:
            for o in rows:
                if o["id"] == n["id"]:
                    continue
                # trong cùng video: chỉ xét mỗi cặp một lần
                if o["video_id"] == video_id and o["idx"] <= n["idx"]:
                    continue
                d = hamming(n["h"], o["h"])
                if d <= max_distance:
                    pairs.append({"a": n["id"], "b": o["id"], "d": d})
        if pairs:
            self._q(
                """
                UNWIND $pairs AS p
                MATCH (a:Segment {id: p.a}), (b:Segment {id: p.b})
                MERGE (a)-[r:SIMILAR_TO]-(b)
                SET r.distance = p.d
                """,
                pairs=pairs,
            )
        return len(pairs)

    def delete_video(self, video_id: str) -> bool:
        rows = self._q(
            """
            MATCH (v:Video {id: $vid})
            OPTIONAL MATCH (v)-[:HAS_SEGMENT]->(s:Segment)
            WITH v, collect(s) AS segs
            FOREACH (x IN segs | DETACH DELETE x)
            DETACH DELETE v
            RETURN count(*) AS n
            """,
            vid=video_id,
        )
        return bool(rows and rows[0]["n"])

    # ---------- đọc ----------
    def list_videos(self) -> list[dict]:
        rows = self._q(
            """
            MATCH (v:Video)
            OPTIONAL MATCH (v)-[:HAS_SEGMENT]->(s:Segment)
            WITH v, count(s) AS segment_count
            ORDER BY v.created_at DESC
            RETURN v {.*} AS video, segment_count
            """
        )
        return [{**r["video"], "segment_count": r["segment_count"]} for r in rows]

    def get_video(self, video_id: str) -> dict | None:
        rows = self._q(
            """
            MATCH (v:Video {id: $vid})
            OPTIONAL MATCH (v)-[:HAS_SEGMENT]->(s:Segment)
            OPTIONAL MATCH (s)-[r:SIMILAR_TO]-(o:Segment)<-[:HAS_SEGMENT]-(ov:Video)
            WITH v, s, collect(CASE WHEN o IS NULL THEN NULL ELSE
                 {id: o.id, video_id: o.video_id, video_name: ov.filename, index: o.index,
                  start_tc: o.start_tc, distance: r.distance} END) AS similar
            ORDER BY s.index
            RETURN v {.*} AS video,
                   collect(CASE WHEN s IS NULL THEN NULL ELSE s {.*, similar: similar} END) AS segments
            """,
            vid=video_id,
        )
        if not rows:
            return None
        return {**rows[0]["video"], "segments": rows[0]["segments"]}

    def segment_at(self, video_id: str, t: float) -> dict | None:
        """Đoạn nào chứa giây thứ t của video?"""
        rows = self._q(
            """
            MATCH (:Video {id: $vid})-[:HAS_SEGMENT]->(s:Segment)
            WHERE s.start_time <= $t AND $t < s.end_time
            RETURN s {.*} AS seg
            ORDER BY s.index LIMIT 1
            """,
            vid=video_id, t=float(t),
        )
        return rows[0]["seg"] if rows else None
