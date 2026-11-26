"""征集办公室、库房、财务共用的捐赠确认后台。

资料端（desk_source.py）提供不可变的原始观察；本服务把三方确认动作追加到事件账，
所有业务视图由 domain.Ledger 派生。服务只监听回环地址，仅使用标准库。

角色边界：
- /office/*   征集办公室：冲突裁决、入藏确认
- /stockroom/* 库房：查验移交、代存交接凭据、归还
- /finance/*  财务：捐赠回执、回执更正
- /pieces /agreements /receipts /donors 为只读对账视图
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from domain import DomainError, Ledger, parse_feeds

FEED_NAMES = ("agreements", "stockroom", "valuation")
READ_DAY_PARAM = "as_of_day"


class EventStore:
    """追加式 JSONL 事件账，每行一个事件。"""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def load(self):
        events = []
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def append(self, event):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()


def _read_feed_directory(directory):
    rows = {}
    for name in FEED_NAMES:
        with (directory / f"{name}.json").open(encoding="utf-8") as stream:
            rows[name] = json.load(stream)
    return rows


def _read_feed_http(origin):
    rows = {}
    for name in FEED_NAMES:
        offset, records = 0, []
        while True:
            with urlopen(
                f"{origin}/sources/{name}?offset={offset}&limit=50", timeout=5
            ) as response:
                page = json.load(response)
            records.extend(page["records"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        rows[name] = records
    return rows


def build_ledger(records_dir=None, upstream=None, event_path=None):
    if upstream:
        rows = _read_feed_http(upstream.rstrip("/"))
    else:
        rows = _read_feed_directory(records_dir)
    bundle = parse_feeds(rows["agreements"], rows["stockroom"], rows["valuation"])
    store = EventStore(event_path) if event_path else None
    events = store.load() if store else []
    return Ledger(bundle, events, store.append if store else None)


class OfficeHandler(BaseHTTPRequestHandler):
    def reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ledger(self):
        return self.server.ledger

    def _as_of_day(self, parameters):
        if READ_DAY_PARAM not in parameters:
            return None
        raw = parameters[READ_DAY_PARAM][0]
        try:
            day = int(raw)
            if day < 0 or len(parameters[READ_DAY_PARAM]) != 1:
                raise ValueError
        except ValueError:
            raise DomainError("invalid_day", "as_of_day 须为非负整数")
        return day

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 1_048_576:
            raise DomainError("invalid_body", "请求体缺失或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("invalid_body", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise DomainError("invalid_body", "请求体须为 JSON 对象")
        return payload

    def _require(self, payload, field, types):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, types):
            raise DomainError("invalid_field", f"字段 {field} 缺失或类型错误",
                              {"field": field})
        return value

    def _day(self, payload):
        day = self._require(payload, "day", int)
        if day < 0:
            raise DomainError("invalid_day", "day 须为非负整数")
        return day

    def do_GET(self):
        url = urlsplit(self.path)
        parameters = parse_qs(url.query, keep_blank_values=True)
        try:
            day = self._as_of_day(parameters)
            self.route_get(url.path, day, parameters)
        except DomainError as error:
            status = {"invalid_day": 400}.get(error.code, 404)
            self.reply(status, self._error_body(error))

    def route_get(self, path, day, parameters):
        ledger = self.ledger()
        if path == "/pieces":
            self.reply(200, {"pieces": ledger.list_pieces(day)})
            return
        if path.startswith("/pieces/") and path.count("/") == 2:
            piece = path.split("/", 2)[2]
            self.reply(200, ledger.piece_view(piece, day))
            return
        if path == "/agreements":
            self.reply(200, {"agreements": ledger.list_agreements(day)})
            return
        if path.startswith("/agreements/") and path.count("/") == 2:
            self.reply(200, ledger.agreement_view(path.split("/", 2)[2], day))
            return
        if path == "/conflicts":
            self.reply(200, {"conflicts": ledger.conflicts(),
                             "label_warnings": ledger.label_warnings()})
            return
        if path == "/receipts":
            self.reply(200, {"receipts": ledger.list_receipts()})
            return
        if path == "/donors":
            self.reply(200, {"donors": ledger.list_donors()})
            return
        if path.startswith("/donors/") and path.count("/") == 2:
            self.reply(200, ledger.reconciliation(path.split("/", 2)[2]))
            return
        self.reply(404, {"error": "无此路径", "code": "not_found"})

    def do_POST(self):
        url = urlsplit(self.path)
        try:
            payload = self._body()
            self.route_post(url.path, payload)
        except DomainError as error:
            status = {
                "invalid_body": 400, "invalid_field": 400, "invalid_day": 400,
                "not_found": 404,
            }.get(error.code, 409)
            self.reply(status, self._error_body(error))

    def route_post(self, path, payload):
        ledger = self.ledger()
        day = self._day(payload)

        if path == "/office/conflicts/resolve":
            event = ledger.resolve_conflict(
                self._require(payload, "piece", str),
                self._require(payload, "agreement", str),
                day, payload.get("note", ""),
            )
            self.reply(201, {"event": event})
            return

        if path == "/office/accessions":
            pieces = payload.get("pieces")
            if pieces is not None and (
                not isinstance(pieces, list)
                or not all(isinstance(piece, str) and piece for piece in pieces)
            ):
                raise DomainError("invalid_field", "pieces 须为非空字符串数组",
                                  {"field": "pieces"})
            event = ledger.accession(
                self._require(payload, "agreement", str), pieces, day)
            self.reply(201, {"event": event})
            return

        if path == "/stockroom/handoffs":
            pieces = self._require(payload, "pieces", list)
            self._check_pieces(pieces)
            event = ledger.handoff(pieces, day, payload.get("note", ""))
            self.reply(201, {"event": event})
            return

        if path == "/stockroom/custody-receipts":
            event = ledger.custody_receipt(
                self._require(payload, "agreement", str),
                self._check_pieces(self._require(payload, "pieces", list)), day)
            self.reply(201, {"event": event})
            return

        if path == "/stockroom/returns":
            pieces = self._check_pieces(self._require(payload, "pieces", list))
            event = ledger.return_pieces(
                pieces, day, self._require(payload, "received_by", str))
            self.reply(201, {"event": event})
            return

        if path == "/finance/receipts":
            pieces = self._check_pieces(self._require(payload, "pieces", list))
            event = ledger.issue_receipt(
                self._require(payload, "agreement", str), pieces, day,
                payload.get("valuation"))
            self.reply(201, {"event": event,
                             "receipt": ledger.receipt(event["id"])})
            return

        if path == "/finance/receipts/correct":
            receipt_id = self._require(payload, "receipt", str)
            event = ledger.correct_receipt(
                receipt_id, day, self._require(payload, "reason", str),
                payload.get("replacement"))
            self.reply(201, {"event": event,
                             "receipt": ledger.receipt(receipt_id)})
            return

        self.reply(404, {"error": "无此路径", "code": "not_found"})

    def _check_pieces(self, pieces):
        if not pieces or not all(isinstance(piece, str) and piece for piece in pieces):
            raise DomainError("invalid_field", "pieces 须为非空字符串数组",
                              {"field": "pieces"})
        if len(set(pieces)) != len(pieces):
            raise DomainError("invalid_field", "pieces 不得重复", {"field": "pieces"})
        return pieces

    def _error_body(self, error):
        body = {"error": error.message, "code": error.code}
        if error.details:
            body["details"] = error.details
        return body

    def log_message(self, format, *args):
        pass


def serve(ledger, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), OfficeHandler)
    server.ledger = ledger
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--records", type=Path,
        default=Path(__file__).with_name("records"),
        help="本地资料端登记目录（默认 ./records）",
    )
    parser.add_argument(
        "--upstream",
        help="资料端服务地址，如 http://127.0.0.1:8765；给出后忽略 --records",
    )
    parser.add_argument(
        "--events", type=Path,
        default=Path(__file__).with_name("back_office_events.jsonl"),
        help="三方确认事件账路径",
    )
    options = parser.parse_args()
    ledger = build_ledger(
        records_dir=options.records, upstream=options.upstream,
        event_path=options.events,
    )
    with serve(ledger, options.port) as server:
        print(f"捐赠确认后台已启动，端口 {server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
