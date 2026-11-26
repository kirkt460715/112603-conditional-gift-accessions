"""捐赠后台的领域规则。

资料端（agreements / stockroom / valuation 三份观察）只记原始事实，本模块把它们
与征集办公室、库房、财务三方的确认事件合并，派生出可查询的业务状态。

关键边界：
- custody 记库房保管事实，ownership 记捐赠协议中的移转范围；代存件永远不进馆藏。
- 外箱按标签签收，实物身份以查验记录中的 piece + mark 为准；同标签多件不得并号。
- 整套（all_or_none）协议在共有人签齐、件数查清且仍在库之前，不得部分入藏。
- group_only 估值只能整套适用，不得拆半平分；独立估值的件可单独出凭据。
"""

import threading
from dataclasses import dataclass


class DomainError(Exception):
    """业务规则拒绝，code 供调用方区分处理。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class GiftAgreement:
    id: str
    donor: str
    pieces: tuple
    basis: str
    all_or_none: bool
    signers: tuple
    required: tuple


@dataclass(frozen=True)
class Signature:
    id: str
    agreement: str
    signer: str
    day: int


@dataclass(frozen=True)
class BoxArrival:
    id: str
    day: int
    items: tuple


@dataclass(frozen=True)
class Inspection:
    id: str
    box: str
    day: int
    witnessed: tuple
    unresolved: tuple


@dataclass(frozen=True)
class PieceArrival:
    id: str
    day: int
    items: tuple


@dataclass(frozen=True)
class ReturnFact:
    id: str
    day: int
    pieces: tuple
    received_by: str
    source: str


@dataclass(frozen=True)
class Valuation:
    id: str
    scope: tuple
    amount: int
    group_only: bool


@dataclass(frozen=True)
class LegacyReceipt:
    id: str
    agreement: str
    scope: tuple
    valuation: str
    amount: int
    issued_day: int


def parse_feeds(agreements_rows, stockroom_rows, valuation_rows):
    """把资料端的原始 JSON 行解析成带类型的事实包。"""
    gifts, signatures = {}, {}
    for row in agreements_rows:
        if row.get("kind") == "signature":
            signatures[row["id"]] = Signature(
                row["id"], row["agreement"], row["signer"], row["day"]
            )
        else:
            gifts[row["id"]] = GiftAgreement(
                row["id"], row["donor"], tuple(row["pieces"]), row["basis"],
                bool(row["all_or_none"]), tuple(row["signers"]), tuple(row["required"]),
            )

    boxes, inspections, arrivals, returns = [], [], [], []
    for row in stockroom_rows:
        if "witnessed" in row or "unresolved" in row:
            inspections.append(Inspection(
                row["id"], row.get("box"), row["day"],
                tuple((w["piece"], w["mark"]) for w in row.get("witnessed", [])),
                tuple(row.get("unresolved", [])),
            ))
        elif "received_by" in row:
            returns.append(ReturnFact(
                row["id"], row["day"], tuple(row["items"]), row["received_by"], "feed",
            ))
        elif row.get("items") and "label" in row["items"][0]:
            boxes.append(BoxArrival(row["id"], row["day"], tuple(row["items"])))
        else:
            arrivals.append(PieceArrival(row["id"], row["day"], tuple(row["items"])))

    valuations, legacy_receipts = {}, {}
    for row in valuation_rows:
        if row.get("kind") == "signed_receipt":
            legacy_receipts[row["id"]] = LegacyReceipt(
                row["id"], row["agreement"], tuple(row["scope"]),
                row["valuation"], row["amount"], row["issued_day"],
            )
        else:
            valuations[row["id"]] = Valuation(
                row["id"], tuple(row["scope"]), row["amount"], bool(row["group_only"]),
            )

    return FeedBundle(gifts, signatures, boxes, inspections, arrivals,
                      returns, valuations, legacy_receipts)


@dataclass
class FeedBundle:
    gifts: dict
    signatures: dict
    boxes: list
    inspections: list
    arrivals: list
    returns: list
    valuations: dict
    legacy_receipts: dict


class Ledger:
    """持有不可变资料包与追加式确认事件，所有命令先校验后入账。"""

    def __init__(self, bundle, events=None, persist=None):
        self.bundle = bundle
        self.events = list(events or [])
        self._persist = persist
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ 入账
    def _next_id(self, prefix):
        count = sum(1 for event in self.events if event["id"].startswith(prefix))
        return f"{prefix}{count + 1}"

    def _record(self, prefix, kind, day, payload, sources):
        event = {
            "id": self._next_id(prefix), "type": kind, "day": day,
            "payload": payload, "sources": list(sources),
        }
        self.events.append(event)
        if self._persist is not None:
            self._persist(event)
        return event

    def current_day(self):
        days = [row.day for row in (
            self.bundle.boxes + self.bundle.inspections
            + self.bundle.arrivals + self.bundle.returns
        )]
        days += [sig.day for sig in self.bundle.signatures.values()]
        days += [rcpt.issued_day for rcpt in self.bundle.legacy_receipts.values()]
        days += [event["day"] for event in self.events]
        return max(days, default=0)

    # ----------------------------------------------------------- 物理占有派生
    def _physical_index(self):
        """返回每件实物的观察时间线，并检测同标签多件的并号风险。"""
        witnessed, unresolved, direct, label_warnings = {}, {}, {}, []
        for inspection in self.bundle.inspections:
            for piece, mark in inspection.witnessed:
                witnessed.setdefault(piece, []).append(
                    {"day": inspection.day, "mark": mark,
                     "box": inspection.box, "source": inspection.id})
            for piece in inspection.unresolved:
                unresolved.setdefault(piece, []).append(
                    {"day": inspection.day, "box": inspection.box, "source": inspection.id})
        for arrival in self.bundle.arrivals:
            for item in arrival.items:
                direct.setdefault(item["piece"], []).append(
                    {"day": arrival.day, "mark": item.get("mark"),
                     "kind": item.get("kind"), "source": arrival.id})
        for box in self.bundle.boxes:
            by_label = {}
            for item in box.items:
                by_label.setdefault(item["label"], set()).add(item["mark"])
            for label, marks in by_label.items():
                if len(marks) > 1:
                    label_warnings.append({
                        "box": box.id, "label": label, "marks": sorted(marks),
                        "code": "duplicate_label",
                        "message": f"外箱 {box.id} 内标签 {label} 对应 {len(marks)} 件实物，"
                                   f"须按 mark 分别登记，不得并为一件",
                    })
        # 同一 piece 若在查验中对应两个 mark，说明并号登记已经发生。
        for piece, rows in witnessed.items():
            marks = {row["mark"] for row in rows}
            if len(marks) > 1:
                label_warnings.append({
                    "piece": piece, "marks": sorted(marks),
                    "code": "merged_identity",
                    "message": f"{piece} 被查验记录对应到多个 mark，存在两件同号并为一件的风险",
                })
        returns = {}
        for fact in self.bundle.returns:
            for piece in fact.pieces:
                returns.setdefault(piece, []).append(
                    {"day": fact.day, "received_by": fact.received_by,
                     "source": fact.id, "origin": fact.source})
        for event in self.events:
            if event["type"] == "return":
                for piece in event["payload"]["pieces"]:
                    returns.setdefault(piece, []).append(
                        {"day": event["day"],
                         "received_by": event["payload"].get("received_by"),
                         "source": event["id"], "origin": "decision"})
        return witnessed, unresolved, direct, returns, label_warnings

    def physical_state(self, piece, day=None):
        """as-of 占有状态：returned / held / unresolved / unseen。"""
        day = self.current_day() if day is None else day
        witnessed, unresolved, direct, returns, _ = self._physical_index()
        returned = [r for r in returns.get(piece, []) if r["day"] <= day]
        if returned:
            return "returned", returned[-1]
        seen = [w for w in witnessed.get(piece, []) if w["day"] <= day]
        seen += [a for a in direct.get(piece, []) if a["day"] <= day]
        if seen:
            return "held", sorted(seen, key=lambda row: row["day"])[-1]
        pending = [u for u in unresolved.get(piece, []) if u["day"] <= day]
        if pending:
            return "unresolved", pending[-1]
        return "unseen", None

    def all_pieces(self):
        pieces = set()
        for gift in self.bundle.gifts.values():
            pieces.update(gift.pieces)
        witnessed, unresolved, direct, _, _ = self._physical_index()
        pieces.update(witnessed)
        pieces.update(unresolved)
        pieces.update(direct)
        return sorted(pieces)

    # ------------------------------------------------------------- 协议与签署
    def signature_status(self, agreement, day=None):
        day = self.current_day() if day is None else day
        effective = set(agreement.signers)
        supplement_days = []
        for sig in self.bundle.signatures.values():
            if sig.agreement == agreement.id and sig.day <= day:
                effective.add(sig.signer)
                supplement_days.append(sig.day)
        missing = sorted(set(agreement.required) - effective)
        complete = not missing
        signature_day = max(supplement_days, default=0) if complete else None
        return {
            "complete": complete, "missing": missing,
            "signers": sorted(effective), "signature_day": signature_day,
        }

    def claims_for(self, piece):
        return [gift for gift in self.bundle.gifts.values() if piece in gift.pieces]

    def conflicts(self):
        """同一编号被多份协议主张时，核对实物与权益是否一致。"""
        witnessed, _, _, _, _ = self._physical_index()
        resolutions = {
            event["payload"]["piece"]: event
            for event in self.events if event["type"] == "conflict_resolution"
        }
        result = []
        for piece in self.all_pieces():
            claims = self.claims_for(piece)
            if len(claims) < 2:
                continue
            bases = {claim.basis for claim in claims}
            donors = {claim.donor for claim in claims}
            if len(bases) > 1:
                reason = "basis_mismatch"
                message = "同一编号在两份协议中承诺的权益种类不同（ownership 与 custody）"
            elif len(donors) > 1:
                reason = "donor_mismatch"
                message = "同一编号由不同共有人分别承诺，须核对签署人与实物同一性"
            else:
                reason = "duplicate_claim"
                message = "同一编号被重复申报，须确认是否为同一件实物"
            entry = {
                "piece": piece, "reason": reason, "message": message,
                "observed_marks": sorted({row["mark"] for row in witnessed.get(piece, [])}),
                "claims": [{
                    "agreement": claim.id, "donor": claim.donor,
                    "basis": claim.basis, "all_or_none": claim.all_or_none,
                } for claim in claims],
                "resolution": None,
            }
            if piece in resolutions:
                event = resolutions[piece]
                entry["resolution"] = {
                    "agreement": event["payload"]["agreement"],
                    "day": event["day"], "note": event["payload"].get("note", ""),
                    "event_id": event["id"],
                }
            result.append(entry)
        return result

    def open_conflict_on(self, piece):
        for conflict in self.conflicts():
            if conflict["piece"] == piece and conflict["resolution"] is None:
                return conflict
        return None

    def agreement_blockers(self, agreement, day, pieces=None):
        """返回截至 day 阻止入藏的原因清单；ownership 协议为空即可入藏。

        pieces 给定时只检查该批申请件（非整套协议的部分入藏）；
        签署条件针对整份协议，始终检查。
        """
        scope = list(agreement.pieces) if pieces is None else list(pieces)
        blockers = []
        if agreement.basis != "ownership":
            blockers.append({
                "code": "custody_not_accessionable",
                "message": "代存协议只转移保管事实，不能确认捐赠入藏",
            })
            return blockers
        status = self.signature_status(agreement, day)
        if not status["complete"]:
            blockers.append({
                "code": "signature_incomplete",
                "message": "共有人尚未签齐", "missing_signers": status["missing"],
            })
        for piece in scope:
            state, detail = self.physical_state(piece, day)
            if state != "held":
                blockers.append({
                    "code": "not_in_possession", "piece": piece,
                    "message": f"{piece} 当前为 {state}，不满足入藏占有条件", "detail": detail,
                })
            conflict = self.open_conflict_on(piece)
            if conflict is not None:
                blockers.append({
                    "code": "conflict_open", "piece": piece,
                    "message": conflict["message"], "reason": conflict["reason"],
                })
        return blockers

    # --------------------------------------------------------------- 确认事件
    def resolve_conflict(self, piece, agreement_id, day, note=""):
        with self.lock:
            claims = self.claims_for(piece)
            if len(claims) < 2:
                raise DomainError("not_a_conflict", f"{piece} 不存在多头主张")
            if self.open_conflict_on(piece) is None:
                raise DomainError("conflict_already_resolved", f"{piece} 的冲突已有处理结论")
            if agreement_id not in {claim.id for claim in claims}:
                raise DomainError("unknown_agreement", "裁决协议未主张该件")
            sources = [claim.id for claim in claims]
            return self._record("resolution-", "conflict_resolution", day,
                                {"piece": piece, "agreement": agreement_id, "note": note},
                                sources)

    def accession(self, agreement_id, pieces, day):
        with self.lock:
            agreement = self._require_agreement(agreement_id)
            if agreement.basis != "ownership":
                raise DomainError("custody_not_accessionable",
                                  "代存件不能作为馆方名下实物入藏")
            requested = tuple(pieces) if pieces else agreement.pieces
            unknown = sorted(set(requested) - set(agreement.pieces))
            if unknown:
                raise DomainError("piece_not_in_agreement",
                                  "申报件不属于该协议", {"pieces": unknown})
            if agreement.all_or_none and set(requested) != set(agreement.pieces):
                raise DomainError("all_or_none_set_required",
                                  "该协议约定整套接收，不得拆开入藏",
                                  {"expected": list(agreement.pieces)})
            blockers = self.agreement_blockers(agreement, day, requested)
            if blockers:
                raise DomainError("accession_blocked", "入藏条件未满足",
                                  {"blockers": blockers})
            done = self._accession_map(day)
            duplicated = sorted(piece for piece in requested if piece in done)
            if duplicated:
                raise DomainError("already_accessioned", "该件已确认入藏",
                                  {"pieces": duplicated})
            sources = [agreement.id]
            for piece in requested:
                state, detail = self.physical_state(piece, day)
                if detail and detail.get("source"):
                    sources.append(detail["source"])
            return self._record("acc-", "accession", day,
                                {"agreement": agreement_id, "pieces": list(requested),
                                 "donor": agreement.donor}, sources)

    def handoff(self, pieces, day, note=""):
        """库房把已查清的件交给后续经办；未查清件不得带入。"""
        with self.lock:
            for piece in pieces:
                state, detail = self.physical_state(piece, day)
                if state != "held":
                    raise DomainError("piece_not_cleared",
                                      f"{piece} 尚未查清或已不在库，不能移交经办",
                                      {"piece": piece, "state": state, "detail": detail})
            sources = []
            for piece in pieces:
                _, detail = self.physical_state(piece, day)
                if detail and detail.get("source"):
                    sources.append(detail["source"])
            return self._record("handoff-", "handoff", day,
                                {"pieces": list(pieces), "note": note}, sources)

    def custody_receipt(self, agreement_id, pieces, day):
        """代存件交接凭据：只证明保管事实，件仍可归还，不产生馆藏身份。"""
        with self.lock:
            agreement = self._require_agreement(agreement_id)
            if agreement.basis != "custody":
                raise DomainError("not_a_custody_agreement",
                                  "交接凭据只适用于代存协议")
            unknown = sorted(set(pieces) - set(agreement.pieces))
            if unknown:
                raise DomainError("piece_not_in_agreement",
                                  "凭据件不属于该代存协议", {"pieces": unknown})
            existing = {
                piece for event in self.events if event["type"] == "custody_receipt"
                for piece in event["payload"]["pieces"]
            }
            for piece in pieces:
                state, detail = self.physical_state(piece, day)
                if state != "held":
                    raise DomainError("piece_not_held",
                                      f"{piece} 当前为 {state}，不能出具代存交接凭据",
                                      {"detail": detail})
                if self.open_conflict_on(piece) is not None:
                    raise DomainError("conflict_open",
                                      f"{piece} 的权益主张尚未查清，不能出具凭据")
                if piece in existing:
                    raise DomainError("custody_receipt_exists",
                                      f"{piece} 已出具交接凭据")
            return self._record("handover-", "custody_receipt", day,
                                {"agreement": agreement_id, "pieces": list(pieces),
                                 "donor": agreement.donor}, [agreement_id])

    def return_pieces(self, pieces, day, received_by):
        """归还：代存/暂放件可退；已入藏成为馆方实物的不在此路径。"""
        with self.lock:
            accessioned = self._accession_map(day)
            for piece in pieces:
                if piece in accessioned:
                    raise DomainError("accessioned_not_returnable",
                                      f"{piece} 已入藏为馆方实物，归还须走注销程序")
                state, detail = self.physical_state(piece, day)
                if state == "returned":
                    raise DomainError("already_returned", f"{piece} 已归还",
                                      {"detail": detail})
                if state != "held":
                    raise DomainError("piece_not_held",
                                      f"{piece} 当前为 {state}，无实物可还",
                                      {"detail": detail})
            sources = []
            for piece in pieces:
                _, detail = self.physical_state(piece, day)
                if detail and detail.get("source"):
                    sources.append(detail["source"])
            return self._record("return-", "return", day,
                                {"pieces": list(pieces), "received_by": received_by},
                                sources)

    # ------------------------------------------------------------------ 回执
    def _accession_map(self, day=None):
        result = {}
        for event in self.events:
            if event["type"] != "accession":
                continue
            if day is not None and event["day"] > day:
                continue
            for piece in event["payload"]["pieces"]:
                result[piece] = event
        return result

    def _effective_receipt_ids(self):
        corrected = {
            event["payload"]["receipt"]
            for event in self.events if event["type"] == "correction"
        }
        effective = set()
        for receipt in self.bundle.legacy_receipts.values():
            if receipt.id not in corrected and not self.audit_legacy(receipt.id):
                effective.add(receipt.id)
        for event in self.events:
            if event["type"] == "receipt" and event["id"] not in corrected:
                effective.add(event["id"])
        return effective

    def _used_valuations(self):
        used = set()
        effective = self._effective_receipt_ids()
        for receipt in self.bundle.legacy_receipts.values():
            if receipt.id in effective:
                used.add(receipt.valuation)
        for event in self.events:
            if event["type"] == "receipt" and event["id"] in effective:
                used.update(event["payload"]["valuations"])
        return used

    def _valuation_cover(self, pieces, pinned_valuation=None):
        target = set(pieces)
        candidates = [v for v in self.bundle.valuations.values()
                      if set(v.scope) <= target]
        if pinned_valuation is not None:
            pinned = self.bundle.valuations.get(pinned_valuation)
            if pinned is None:
                raise DomainError("unknown_valuation", "估值编号不存在")
            if set(pinned.scope) > target:
                raise DomainError("group_only_split",
                                  "组套估值只适用于完整组套，不得拆开确认",
                                  {"valuation": pinned.id, "scope": list(pinned.scope)})
        covers = []

        def search(remaining, chosen):
            if not remaining:
                covers.append(chosen)
                return
            pivot = min(remaining)
            for valuation in sorted(candidates, key=lambda v: v.id):
                if pivot in valuation.scope and set(valuation.scope) <= remaining:
                    search(remaining - set(valuation.scope), chosen + [valuation.id])

        search(target, [])
        if pinned_valuation is not None:
            covers = [cover for cover in covers if pinned_valuation in cover]
        if not covers:
            partial = [v.id for v in self.bundle.valuations.values()
                       if set(v.scope) & target and not set(v.scope) <= target]
            if partial:
                raise DomainError("group_only_split",
                                  "组套估值只适用于完整组套，不得原价平分",
                                  {"valuations": partial})
            raise DomainError("no_applicable_valuation",
                              "该批件没有适用的独立估值")
        covers.sort(key=lambda ids: (len(ids), ids))
        return covers[0]

    def issue_receipt(self, agreement_id, pieces, day, valuation_id=None):
        with self.lock:
            agreement = self._require_agreement(agreement_id)
            if agreement.basis != "ownership":
                raise DomainError("custody_not_receiptable",
                                  "代存件不确认捐赠权益，不能开具捐赠回执")
            requested = list(pieces)
            unknown = sorted(set(requested) - set(agreement.pieces))
            if unknown:
                raise DomainError("piece_not_in_agreement",
                                  "回执件不属于该协议", {"pieces": unknown})
            accessioned = self._accession_map(day)
            not_done = sorted(piece for piece in requested if piece not in accessioned)
            if not_done:
                raise DomainError("not_accessioned",
                                  "财务只对确已入藏的件确认回执",
                                  {"pieces": not_done})
            wrong_agreement = sorted(
                piece for piece in requested
                if accessioned[piece]["payload"]["agreement"] != agreement_id
            )
            if wrong_agreement:
                raise DomainError("agreement_mismatch",
                                  "入藏依据与回执协议不一致", {"pieces": wrong_agreement})
            cover = self._valuation_cover(requested, valuation_id)
            used = self._used_valuations()
            reused = sorted(set(cover) & used)
            if reused:
                raise DomainError("valuation_already_used",
                                  "估值已被有效回执占用", {"valuations": reused})
            amount = sum(self.bundle.valuations[vid].amount for vid in cover)
            return self._record("receipt-", "receipt", day, {
                "agreement": agreement_id, "donor": agreement.donor,
                "pieces": requested, "valuations": cover, "amount": amount,
            }, [agreement_id] + cover)

    def audit_legacy(self, receipt_id):
        """对资料端已开出的历史回执做事后审计，返回违规代码清单。"""
        receipt = self.bundle.legacy_receipts.get(receipt_id)
        if receipt is None:
            raise DomainError("unknown_receipt", "回执不存在")
        findings = []
        accessioned = self._accession_map(receipt.issued_day)
        missing = sorted(piece for piece in receipt.scope if piece not in accessioned)
        if missing:
            findings.append({
                "code": "not_accessioned_at_issue",
                "message": "开具回执时实物尚未确已入藏", "pieces": missing,
            })
        valuation = self.bundle.valuations.get(receipt.valuation)
        if valuation is not None and valuation.group_only \
                and set(receipt.scope) != set(valuation.scope):
            findings.append({
                "code": "group_only_split",
                "message": "组套估值被拆开确认，整套范围为 " + ", ".join(valuation.scope),
                "valuation": valuation.id,
            })
            if receipt.amount != valuation.amount:
                findings.append({
                    "code": "amount_mismatch",
                    "message": "拆半金额无效，组套整套金额为 "
                               f"{valuation.amount}，回执却开 {receipt.amount}",
                    "valuation": valuation.id,
                    "expected_amount": valuation.amount,
                    "actual_amount": receipt.amount,
                })
        return findings

    def correct_receipt(self, receipt_id, day, reason, replacement_id=None):
        """更正回执：原回执作废，登记差额；可挂接新开的替代回执。"""
        with self.lock:
            target = self._receipt_record(receipt_id)
            if target["status"] == "superseded":
                raise DomainError("receipt_already_corrected",
                                  "该回执已办理更正")
            if replacement_id is not None:
                replacement = self._receipt_record(replacement_id)
                if replacement["status"] != "effective":
                    raise DomainError("replacement_not_effective",
                                      "替代回执本身不是有效回执")
            delta = -target["amount"]
            return self._record("correction-", "correction", day, {
                "receipt": receipt_id, "reason": reason, "delta": delta,
                "replacement": replacement_id,
            }, [receipt_id])

    def _require_agreement(self, agreement_id):
        agreement = self.bundle.gifts.get(agreement_id)
        if agreement is None:
            raise DomainError("unknown_agreement", "协议不存在")
        return agreement

    def _receipt_record(self, receipt_id):
        corrections = [{
            "event_id": event["id"], "day": event["day"],
            "reason": event["payload"]["reason"],
            "delta": event["payload"]["delta"],
            "replacement": event["payload"].get("replacement"),
        } for event in self.events
            if event["type"] == "correction"
            and event["payload"]["receipt"] == receipt_id]
        if receipt_id in self.bundle.legacy_receipts:
            legacy = self.bundle.legacy_receipts[receipt_id]
            findings = self.audit_legacy(receipt_id)
            corrected = bool(corrections)
            status = "superseded" if corrected else ("invalid" if findings else "effective")
            record = {
                "id": legacy.id, "source": "legacy", "agreement": legacy.agreement,
                "scope": list(legacy.scope), "valuation": legacy.valuation,
                "amount": legacy.amount, "issued_day": legacy.issued_day,
                "status": status, "audit": findings,
            }
        else:
            for event in self.events:
                if event["type"] == "receipt" and event["id"] == receipt_id:
                    record = {
                        "id": event["id"], "source": "issued",
                        "agreement": event["payload"]["agreement"],
                        "scope": list(event["payload"]["pieces"]),
                        "valuations": list(event["payload"]["valuations"]),
                        "amount": event["payload"]["amount"], "issued_day": event["day"],
                        "donor": event["payload"]["donor"],
                        "status": "superseded" if corrections else "effective",
                        "audit": [],
                    }
                    break
            else:
                raise DomainError("unknown_receipt", "回执不存在")
        record["corrections"] = corrections
        return record

    # ------------------------------------------------------------------ 视图
    def piece_view(self, piece, day=None):
        if piece not in self.all_pieces():
            raise DomainError("unknown_piece", "实物编号不在任何登记中")
        day = self.current_day() if day is None else day
        witnessed, unresolved, direct, returns, warnings = self._physical_index()
        state, detail = self.physical_state(piece, day)
        accessioned = self._accession_map(day)
        custody_docs = [
            event for event in self.events
            if event["type"] == "custody_receipt" and piece in event["payload"]["pieces"]
        ]
        conflict = next((c for c in self.conflicts() if c["piece"] == piece), None)
        return {
            "piece": piece, "as_of_day": day,
            "physical": {
                "state": state, "detail": detail,
                "witnessed": witnessed.get(piece, []),
                "arrivals": direct.get(piece, []),
                "unresolved": unresolved.get(piece, []),
                "returns": returns.get(piece, []),
            },
            "label_warnings": [w for w in warnings
                               if w.get("piece") == piece],
            "claims": [{
                "agreement": claim.id, "donor": claim.donor, "basis": claim.basis,
                "all_or_none": claim.all_or_none,
                "signature": self.signature_status(claim, day),
            } for claim in self.claims_for(piece)],
            "conflict": conflict,
            "custody_receipts": [event["id"] for event in custody_docs],
            "accession": None if piece not in accessioned else {
                "agreement": accessioned[piece]["payload"]["agreement"],
                "day": accessioned[piece]["day"],
                "event_id": accessioned[piece]["id"],
            },
            "valuations": [{
                "id": v.id, "scope": list(v.scope), "amount": v.amount,
                "group_only": v.group_only,
            } for v in self.bundle.valuations.values() if piece in v.scope],
        }

    def list_pieces(self, day=None):
        day = self.current_day() if day is None else day
        return [self.piece_view(piece, day) for piece in self.all_pieces()]

    def agreement_view(self, agreement_id, day=None):
        agreement = self._require_agreement(agreement_id)
        day = self.current_day() if day is None else day
        status = self.signature_status(agreement, day)
        pieces = []
        for piece in agreement.pieces:
            state, detail = self.physical_state(piece, day)
            pieces.append({"piece": piece, "state": state, "detail": detail,
                           "conflict_open": self.open_conflict_on(piece) is not None})
        blockers = self.agreement_blockers(agreement, day)
        return {
            "id": agreement.id, "donor": agreement.donor, "basis": agreement.basis,
            "all_or_none": agreement.all_or_none, "pieces_scope": list(agreement.pieces),
            "as_of_day": day, "signature": status,
            "piece_states": pieces,
            "blockers": blockers,
            "eligible_for_accession": not blockers,
        }

    def list_agreements(self, day=None):
        day = self.current_day() if day is None else day
        return [self.agreement_view(gift.id, day)
                for gift in self.bundle.gifts.values()]

    def list_receipts(self):
        records = [self.receipt(rid) for rid in self.bundle.legacy_receipts]
        records += [self.receipt(event["id"])
                    for event in self.events if event["type"] == "receipt"]
        return records

    def receipt(self, receipt_id):
        """单张回执的当前状态、审计发现与更正差额记录。"""
        return self._receipt_record(receipt_id)

    def reconciliation(self, donor):
        """供捐赠人对账：待查、馆方接收、退回、有效回执与更正差额。"""
        agreements = [g for g in self.bundle.gifts.values() if g.donor == donor]
        if not agreements:
            raise DomainError("unknown_donor", "该捐赠人没有协议记录")
        day = self.current_day()
        accessioned = self._accession_map(day)
        pending, held_custody, returned_pieces, received = [], [], [], []
        resolved_away = []
        own_agreements = {agreement.id for agreement in agreements}
        for agreement in agreements:
            for piece in agreement.pieces:
                state, _ = self.physical_state(piece, day)
                if piece in accessioned:
                    entry = {
                        "piece": piece,
                        "agreement": accessioned[piece]["payload"]["agreement"],
                        "day": accessioned[piece]["day"],
                    }
                    # 只有以本捐赠人自己的协议入藏，才算馆方接收其捐赠；
                    # 冲突后裁决给他人的件单独列示，避免把别人的入藏计到其名下。
                    if entry["agreement"] in own_agreements:
                        received.append(entry)
                    else:
                        resolved_away.append(
                            {"piece": piece, "own_agreement": agreement.id,
                             "accessioned_under": entry["agreement"]})
                elif state == "returned":
                    returned_pieces.append({"piece": piece, "agreement": agreement.id})
                elif agreement.basis == "custody" and state == "held":
                    held_custody.append({"piece": piece, "agreement": agreement.id})
                else:
                    view = self.agreement_view(agreement.id, day)
                    reasons = [b["code"] for b in view["blockers"]]
                    pending.append({"piece": piece, "agreement": agreement.id,
                                    "state": state,
                                    "reasons": reasons or ["awaiting_review"]})
        receipts = [r for r in self.list_receipts()
                    if self._receipt_donor(r["id"]) == donor]
        effective = [r for r in receipts if r["status"] == "effective"]
        return {
            "donor": donor, "as_of_day": day,
            "pending": pending,
            "received_by_museum": received,
            "in_custody": held_custody,
            "returned": returned_pieces,
            "resolved_under_other_agreement": resolved_away,
            "receipts": receipts,
            "effective_receipt_total": sum(r["amount"] for r in effective),
            "outstanding_correction_total": sum(
                c["delta"] for r in receipts for c in r["corrections"]),
        }

    def _receipt_donor(self, receipt_id):
        if receipt_id in self.bundle.legacy_receipts:
            agreement_id = self.bundle.legacy_receipts[receipt_id].agreement
        else:
            event = next(e for e in self.events
                         if e["type"] == "receipt" and e["id"] == receipt_id)
            agreement_id = event["payload"]["agreement"]
        return self.bundle.gifts[agreement_id].donor

    def list_donors(self):
        return sorted({gift.donor for gift in self.bundle.gifts.values()})

    def label_warnings(self):
        return self._physical_index()[4]
