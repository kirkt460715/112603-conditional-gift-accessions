"""后台 HTTP 合同、事件账持久化与资料端联调回归。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from back_office import build_ledger, serve
from desk_source import open_desk

RECORDS = Path(__file__).with_name("records")


class BackOfficeHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.event_path = Path(self.temp.name) / "events.jsonl"
        self.ledger = build_ledger(RECORDS, None, self.event_path)
        self.server = serve(self.ledger, 0)
        self.thread = Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, suffix):
        with urlopen(self.origin + suffix, timeout=2) as response:
            self.assertEqual(response.headers.get_content_charset(), "utf-8")
            return json.load(response)

    def post(self, suffix, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.origin + suffix, data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def post_expect_error(self, suffix, payload, code):
        try:
            self.post(suffix, payload)
        except HTTPError as error:
            body = json.load(error)
            self.assertEqual(body["code"], code)
            return error.code, body
        self.fail(f"应当以 {code} 失败")

    def test_read_views_and_as_of_day(self):
        pieces = self.get("/pieces")["pieces"]
        self.assertEqual(len(pieces), 4)
        view = self.get("/pieces/sheet-51a?as_of_day=3")
        self.assertEqual(view["physical"]["state"], "unseen")
        warnings = self.get("/conflicts")
        self.assertEqual(warnings["label_warnings"][0]["code"], "duplicate_label")
        self.assertEqual(warnings["conflicts"][0]["reason"], "basis_mismatch")
        self.get("/agreements")
        self.get("/agreements/gift-17?as_of_day=5")
        donors = self.get("/donors")["donors"]
        self.assertEqual(set(donors), {"a-7", "a-9"})
        self.get("/donors/a-9")

    def test_bad_requests_and_missing_paths(self):
        with self.assertRaises(HTTPError) as bad_day:
            self.get("/pieces?as_of_day=-2")
        self.assertEqual(bad_day.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            self.get("/pieces/ghost")
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(json.load(missing.exception)["code"], "unknown_piece")
        with self.assertRaises(HTTPError) as no_path:
            self.post("/nowhere", {"day": 1})
        self.assertEqual(no_path.exception.code, 404)
        with self.assertRaises(HTTPError) as bad_body:
            self.post("/office/accessions", {"day": "eight"})
        self.assertEqual(bad_body.exception.code, 400)
        with self.assertRaises(HTTPError) as no_body:
            self.post("/office/accessions", {})
        self.assertEqual(no_body.exception.code, 400)

    def test_full_three_role_flow_over_http(self):
        # 库房：代存交接凭据（冲突未查清的件被拒）
        self.post_expect_error("/stockroom/custody-receipts", {
            "day": 5, "agreement": "gift-18", "pieces": ["sheet-51a"]},
            "conflict_open")
        status, handover = self.post("/stockroom/custody-receipts", {
            "day": 5, "agreement": "gift-18", "pieces": ["sheet-51b"]})
        self.assertEqual(status, 201)
        # 征集办公室：未满足条件时入藏被拒，裁决冲突后整套入藏
        self.post_expect_error("/office/accessions",
                               {"day": 5, "agreement": "gift-17"},
                               "accession_blocked")
        self.post("/office/conflicts/resolve", {
            "day": 8, "piece": "sheet-51a", "agreement": "gift-17",
            "note": "确认同一 blue 印样"})
        status, accession = self.post("/office/accessions",
                                      {"day": 8, "agreement": "gift-17"})
        self.assertEqual(status, 201)
        self.assertEqual(accession["event"]["payload"]["pieces"],
                         ["plate-51", "sheet-51a"])
        # 财务：拆半被拒，整套回执成立
        self.post_expect_error("/finance/receipts", {
            "day": 8, "agreement": "gift-17", "pieces": ["sheet-51a"]},
            "group_only_split")
        status, receipt = self.post("/finance/receipts", {
            "day": 8, "agreement": "gift-17",
            "pieces": ["plate-51", "sheet-51a"]})
        self.assertEqual(receipt["receipt"]["amount"], 86400)
        # 后续批次：sheet-77 独立入藏与回执
        self.post("/office/accessions", {"day": 10, "agreement": "gift-22"})
        status, receipt22 = self.post("/finance/receipts", {
            "day": 10, "agreement": "gift-22", "pieces": ["sheet-77"]})
        self.assertEqual(receipt22["receipt"]["amount"], 25200)
        # 历史错误回执的更正与差额可查
        status, correction = self.post("/finance/receipts/correct", {
            "day": 10, "receipt": "gift-ack-1",
            "reason": "入藏前开具且组套估值拆半",
            "replacement": receipt["event"]["id"]})
        self.assertEqual(correction["event"]["payload"]["delta"], -43200)
        receipts = {r["id"]: r for r in self.get("/receipts")["receipts"]}
        self.assertEqual(receipts["gift-ack-1"]["status"], "superseded")
        self.assertTrue(receipts["gift-ack-1"]["corrections"])
        # 库房视图：代存件已退回，凭据仍可追溯
        view = self.get("/pieces/sheet-51b")
        self.assertEqual(view["physical"]["state"], "returned")
        self.assertEqual(view["custody_receipts"], [handover["event"]["id"]])
        # 捐赠人对账：边界各自正确
        donor9 = self.get("/donors/a-9")
        self.assertEqual(donor9["received_by_museum"], [])
        self.assertEqual(
            donor9["resolved_under_other_agreement"][0]["accessioned_under"],
            "gift-17")

    def test_duplicate_action_is_idempotently_rejected(self):
        status, _ = self.post("/office/accessions",
                              {"day": 10, "agreement": "gift-22"})
        self.assertEqual(status, 201)
        self.post_expect_error("/office/accessions",
                               {"day": 10, "agreement": "gift-22"},
                               "already_accessioned")

    def test_events_persisted_and_replay_rebuilds_ledger(self):
        self.post("/office/conflicts/resolve", {
            "day": 8, "piece": "sheet-51a", "agreement": "gift-17", "note": "ok"})
        _, accession = self.post("/office/accessions",
                                 {"day": 8, "agreement": "gift-17"})
        event_path = self.event_path.resolve()
        raw = [json.loads(line) for line in event_path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(event["type"] == "accession" for event in raw))
        rebuilt = build_ledger(RECORDS, None, event_path)
        self.assertEqual(
            [event["id"] for event in rebuilt.events],
            [event["id"] for event in self.ledger.events])
        self.assertEqual(rebuilt.list_receipts(), self.ledger.list_receipts())
        self.assertEqual(
            rebuilt.piece_view("sheet-51a")["accession"]["event_id"],
            accession["event"]["id"])
        # 重建后幂等保护仍在：重复入藏被拒
        self.post_expect_error("/office/accessions",
                               {"day": 11, "agreement": "gift-17"},
                               "already_accessioned")


class UpstreamIntegrationTest(unittest.TestCase):
    """后台经 HTTP 从资料端分页拉取三份登记。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.desk = open_desk(RECORDS, 0)
        self.desk_thread = Thread(target=self.desk.serve_forever,
                                  kwargs={"poll_interval": 0.01})
        self.desk_thread.start()
        self.upstream = f"http://127.0.0.1:{self.desk.server_port}"
        self.event_path = Path(self.temp.name) / "upstream-events.jsonl"

    def tearDown(self):
        self.desk.shutdown()
        self.desk.server_close()
        self.desk_thread.join()
        self.temp.cleanup()

    def test_build_from_http_feeds_matches_directory(self):
        http_ledger = build_ledger(None, self.upstream, self.event_path)
        local_ledger = build_ledger(RECORDS, None, None)
        self.assertEqual(http_ledger.all_pieces(), local_ledger.all_pieces())
        self.assertEqual(http_ledger.conflicts(), local_ledger.conflicts())
        self.assertEqual(
            http_ledger.audit_legacy("gift-ack-1"),
            local_ledger.audit_legacy("gift-ack-1"))
        # 通过拉取来的数据同样可以办理确认
        http_ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        event = http_ledger.accession("gift-17", None, 8)
        self.assertEqual(event["payload"]["pieces"], ["plate-51", "sheet-51a"])


if __name__ == "__main__":
    unittest.main()
