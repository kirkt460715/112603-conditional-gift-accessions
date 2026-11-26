"""领域规则回归：暂存与入藏边界、整套条件、冲突裁决、估值与回执更正。"""

import unittest

from domain import DomainError, Ledger, parse_feeds
import json
from pathlib import Path

RECORDS = Path(__file__).with_name("records")


def load_bundle():
    rows = {
        name: json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("agreements", "stockroom", "valuation")
    }
    return parse_feeds(rows["agreements"], rows["stockroom"], rows["valuation"])


def fresh_ledger():
    return Ledger(load_bundle())


class SyntheticBundleMixin:
    """构造与资料端同构的合成登记，用于覆盖资料中没有的分支。"""

    @staticmethod
    def ledger(agreements, stockroom, valuation):
        return Ledger(parse_feeds(agreements, stockroom, valuation))


class PhysicalStateTest(unittest.TestCase, SyntheticBundleMixin):
    def test_box_label_does_not_create_pieces_and_duplicate_label_warns(self):
        ledger = fresh_ledger()
        # 外箱标签 S51/P51 不是藏品编号；实物身份来自查验记录
        self.assertEqual(
            set(ledger.all_pieces()),
            {"plate-51", "sheet-51a", "sheet-51b", "sheet-77"},
        )
        warnings = ledger.label_warnings()
        self.assertEqual([w["code"] for w in warnings], ["duplicate_label"])
        self.assertEqual(warnings[0]["marks"], ["black", "blue"])

    def test_as_of_possession_timeline(self):
        ledger = fresh_ledger()
        # day 3 箱子签收，但还没查验，任何件都不能算在库
        self.assertEqual(ledger.physical_state("sheet-51a", 3)[0], "unseen")
        # day 4 查验到 blue/black 两件，plate-51 仍不符
        self.assertEqual(ledger.physical_state("sheet-51a", 4)[0], "held")
        self.assertEqual(ledger.physical_state("sheet-51b", 4)[0], "held")
        self.assertEqual(ledger.physical_state("plate-51", 4)[0], "unresolved")
        # day 8 补查验后查清
        self.assertEqual(ledger.physical_state("plate-51", 8)[0], "held")
        # day 9 资料端登记的归还生效
        self.assertEqual(ledger.physical_state("sheet-51b", 9)[0], "returned")
        # day 10 单独到件
        self.assertEqual(ledger.physical_state("sheet-77", 9)[0], "unseen")
        self.assertEqual(ledger.physical_state("sheet-77", 10)[0], "held")

    def test_merged_identity_warning_when_two_marks_share_piece_id(self):
        agreements = [{
            "id": "g1", "donor": "d1", "pieces": ["p1"], "basis": "ownership",
            "all_or_none": False, "signers": ["d1"], "required": ["d1"],
        }]
        stockroom = [
            {"id": "box1", "day": 1, "items": [
                {"label": "L1", "mark": "m1", "kind": "impression"},
                {"label": "L1", "mark": "m2", "kind": "impression"}]},
            {"id": "in1", "box": "box1", "day": 2, "witnessed": [
                {"piece": "p1", "mark": "m1"}, {"piece": "p1", "mark": "m2"}]},
        ]
        ledger = self.ledger(agreements, stockroom, [])
        codes = {w["code"] for w in ledger.label_warnings()}
        self.assertIn("merged_identity", codes)


class AccessionRuleTest(unittest.TestCase):
    def test_all_or_none_waits_for_cosigner_inspection_and_resolution(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as early:
            ledger.accession("gift-17", None, 5)
        codes = {b["code"] for b in early.exception.details["blockers"]}
        self.assertEqual(
            codes,
            {"signature_incomplete", "not_in_possession", "conflict_open"},
        )
        # day 7 共有人补签，但 plate-51 未查清、冲突未裁决
        with self.assertRaises(DomainError) as day7:
            ledger.accession("gift-17", None, 7)
        codes = {b["code"] for b in day7.exception.details["blockers"]}
        self.assertEqual(codes, {"not_in_possession", "conflict_open"})
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "确认同一 blue 印样")
        event = ledger.accession("gift-17", None, 8)
        self.assertEqual(event["payload"]["pieces"], ["plate-51", "sheet-51a"])

    def test_all_or_none_cannot_be_split(self):
        ledger = fresh_ledger()
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        with self.assertRaises(DomainError) as error:
            ledger.accession("gift-17", ["sheet-51a"], 8)
        self.assertEqual(error.exception.code, "all_or_none_set_required")

    def test_custody_agreement_never_accessions(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as error:
            ledger.accession("gift-18", ["sheet-51b"], 8)
        self.assertEqual(error.exception.code, "custody_not_accessionable")

    def test_non_set_pieces_accession_independently_after_arrival(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError):
            ledger.accession("gift-22", None, 9)
        event = ledger.accession("gift-22", None, 10)
        self.assertEqual(event["payload"]["pieces"], ["sheet-77"])

    def test_unknown_piece_rejected(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as error:
            ledger.accession("gift-22", ["sheet-51a"], 10)
        self.assertEqual(error.exception.code, "piece_not_in_agreement")

    def test_conflict_resolution_guards(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as no_conflict:
            ledger.resolve_conflict("sheet-77", "gift-22", 10)
        self.assertEqual(no_conflict.exception.code, "not_a_conflict")
        with self.assertRaises(DomainError) as wrong_agreement:
            ledger.resolve_conflict("sheet-51a", "gift-22", 8)
        self.assertEqual(wrong_agreement.exception.code, "unknown_agreement")
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        with self.assertRaises(DomainError) as twice:
            ledger.resolve_conflict("sheet-51a", "gift-18", 9)
        self.assertEqual(twice.exception.code, "conflict_already_resolved")


class CustodyAndReturnTest(unittest.TestCase):
    def test_custody_receipt_is_returnable_and_not_collection(self):
        ledger = fresh_ledger()
        # 冲突未查清前，sheet-51a 不能出代存凭据
        with self.assertRaises(DomainError) as blocked:
            ledger.custody_receipt("gift-18", ["sheet-51a"], 5)
        self.assertEqual(blocked.exception.code, "conflict_open")
        event = ledger.custody_receipt("gift-18", ["sheet-51b"], 5)
        self.assertEqual(event["type"], "custody_receipt")
        view = ledger.piece_view("sheet-51b")
        self.assertEqual(view["custody_receipts"], [event["id"]])
        self.assertIsNone(view["accession"])
        # 资料端 day 9 的归还后，凭据仍可追溯而实物已不在库
        view = ledger.piece_view("sheet-51b", 9)
        self.assertEqual(view["physical"]["state"], "returned")
        self.assertEqual(view["custody_receipts"], [event["id"]])

    def test_decision_return_of_held_piece_and_receipt_guard(self):
        agreements = [{
            "id": "g1", "donor": "d1", "pieces": ["p1"], "basis": "custody",
            "all_or_none": False, "signers": ["d1"], "required": ["d1"],
        }]
        stockroom = [{"id": "a1", "day": 2, "items": [
            {"piece": "p1", "mark": "m1", "kind": "impression"}]}]
        ledger = SyntheticBundleMixin.ledger(agreements, stockroom, [])
        ledger.custody_receipt("g1", ["p1"], 3)
        with self.assertRaises(DomainError) as not_held:
            ledger.custody_receipt("g1", ["p1"], 4)
        self.assertEqual(not_held.exception.code, "custody_receipt_exists")
        event = ledger.return_pieces(["p1"], 5, "d1")
        self.assertEqual(event["payload"]["received_by"], "d1")
        with self.assertRaises(DomainError) as again:
            ledger.return_pieces(["p1"], 6, "d1")
        self.assertEqual(again.exception.code, "already_returned")

    def test_accessioned_piece_cannot_return_through_stockroom_path(self):
        ledger = fresh_ledger()
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        ledger.accession("gift-17", None, 8)
        with self.assertRaises(DomainError) as error:
            ledger.return_pieces(["sheet-51a"], 9, "a-7")
        self.assertEqual(error.exception.code, "accessioned_not_returnable")

    def test_handoff_only_carries_cleared_pieces(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as early:
            ledger.handoff(["plate-51"], 4)
        self.assertEqual(early.exception.code, "piece_not_cleared")
        event = ledger.handoff(["plate-51", "sheet-51a"], 8, "交后续经办")
        self.assertEqual(event["payload"]["pieces"], ["plate-51", "sheet-51a"])


class ValuationAndReceiptTest(unittest.TestCase):
    def _accessioned(self):
        ledger = fresh_ledger()
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        ledger.accession("gift-17", None, 8)
        ledger.accession("gift-22", None, 10)
        return ledger

    def test_group_only_valuation_cannot_be_split(self):
        ledger = self._accessioned()
        with self.assertRaises(DomainError) as error:
            ledger.issue_receipt("gift-17", ["sheet-51a"], 8)
        self.assertEqual(error.exception.code, "group_only_split")

    def test_full_group_receipt_uses_whole_amount_once(self):
        ledger = self._accessioned()
        event = ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 8)
        self.assertEqual(event["payload"]["amount"], 86400)
        self.assertEqual(event["payload"]["valuations"], ["v-1"])
        with self.assertRaises(DomainError) as reused:
            ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 9)
        self.assertEqual(reused.exception.code, "valuation_already_used")

    def test_independent_valuation_supports_single_piece_receipt(self):
        ledger = self._accessioned()
        event = ledger.issue_receipt("gift-22", ["sheet-77"], 10)
        self.assertEqual(event["payload"]["amount"], 25200)
        self.assertEqual(event["payload"]["valuations"], ["v-5"])

    def test_receipt_requires_accession_and_matching_agreement(self):
        ledger = fresh_ledger()
        with self.assertRaises(DomainError) as not_done:
            ledger.issue_receipt("gift-22", ["sheet-77"], 10)
        self.assertEqual(not_done.exception.code, "not_accessioned")

    def test_legacy_receipt_audit_finds_all_three_violations(self):
        ledger = fresh_ledger()
        findings = ledger.audit_legacy("gift-ack-1")
        self.assertEqual(
            {f["code"] for f in findings},
            {"not_accessioned_at_issue", "group_only_split", "amount_mismatch"},
        )
        record = ledger._receipt_record("gift-ack-1")
        self.assertEqual(record["status"], "invalid")

    def test_correction_supersedes_with_delta_and_replacement_link(self):
        ledger = self._accessioned()
        replacement = ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 8)
        correction = ledger.correct_receipt(
            "gift-ack-1", 10, "入藏前开具且组套估值拆半", replacement["id"])
        self.assertEqual(correction["payload"]["delta"], -43200)
        record = ledger._receipt_record("gift-ack-1")
        self.assertEqual(record["status"], "superseded")
        with self.assertRaises(DomainError) as twice:
            ledger.correct_receipt("gift-ack-1", 11, "重复更正")
        self.assertEqual(twice.exception.code, "receipt_already_corrected")
        # 被替代的旧回执不再占用估值；但新回执仍占用 v-1
        with self.assertRaises(DomainError) as reused:
            ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 11)
        self.assertEqual(reused.exception.code, "valuation_already_used")

    def test_invalid_legacy_receipt_can_be_corrected_without_replacement(self):
        # 历史违规回执处于 invalid 时即可更正；无替代回执也允许，差额单独可查
        ledger = self._accessioned()
        self.assertEqual(ledger.receipt("gift-ack-1")["status"], "invalid")
        correction = ledger.correct_receipt(
            "gift-ack-1", 9, "先撤回，待入藏与估值条件满足后重开")
        self.assertIsNone(correction["payload"]["replacement"])
        record = ledger.receipt("gift-ack-1")
        self.assertEqual(record["status"], "superseded")
        self.assertEqual(record["corrections"][0]["delta"], -43200)
        # 旧回执作废后释放组套估值，可重新整套开具
        event = ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 9)
        self.assertEqual(event["payload"]["amount"], 86400)

    def test_no_valuation_available_is_rejected(self):
        agreements = [{
            "id": "g1", "donor": "d1", "pieces": ["p9"], "basis": "ownership",
            "all_or_none": False, "signers": ["d1"], "required": ["d1"],
        }]
        stockroom = [{"id": "a1", "day": 1, "items": [
            {"piece": "p9", "mark": "m", "kind": "impression"}]}]
        ledger = SyntheticBundleMixin.ledger(agreements, stockroom, [])
        ledger.accession("g1", None, 2)
        with self.assertRaises(DomainError) as error:
            ledger.issue_receipt("g1", ["p9"], 2)
        self.assertEqual(error.exception.code, "no_applicable_valuation")

    def test_independent_valuations_tile_multi_piece_receipt(self):
        # 两件各有独立估值时，回执按独立估值组合，金额相加且各自只占用一次
        agreements = [{
            "id": "g1", "donor": "d1", "pieces": ["p1", "p2"], "basis": "ownership",
            "all_or_none": False, "signers": ["d1"], "required": ["d1"],
        }]
        stockroom = [{"id": "a1", "day": 1, "items": [
            {"piece": "p1", "mark": "m1", "kind": "impression"},
            {"piece": "p2", "mark": "m2", "kind": "impression"}]}]
        valuation = [
            {"id": "v1", "scope": ["p1"], "amount": 1000, "group_only": False},
            {"id": "v2", "scope": ["p2"], "amount": 2000, "group_only": False},
        ]
        ledger = SyntheticBundleMixin.ledger(agreements, stockroom, valuation)
        ledger.accession("g1", None, 2)
        event = ledger.issue_receipt("g1", ["p1", "p2"], 2)
        self.assertEqual(event["payload"]["valuations"], ["v1", "v2"])
        self.assertEqual(event["payload"]["amount"], 3000)
        with self.assertRaises(DomainError) as reused:
            ledger.issue_receipt("g1", ["p1"], 3)
        self.assertEqual(reused.exception.code, "valuation_already_used")

    def test_group_only_valuation_blocks_receipt_when_partner_missing(self):
        # 组套里另一件未入藏时，组套估值对单件不可用且没有独立估值可用
        agreements = [{
            "id": "g1", "donor": "d1", "pieces": ["p1", "p2"], "basis": "ownership",
            "all_or_none": False, "signers": ["d1"], "required": ["d1"],
        }]
        stockroom = [{"id": "a1", "day": 1, "items": [
            {"piece": "p1", "mark": "m1", "kind": "impression"}]}]
        valuation = [
            {"id": "vg", "scope": ["p1", "p2"], "amount": 9000, "group_only": True},
        ]
        ledger = SyntheticBundleMixin.ledger(agreements, stockroom, valuation)
        ledger.accession("g1", ["p1"], 2)
        with self.assertRaises(DomainError) as error:
            ledger.issue_receipt("g1", ["p1"], 2)
        self.assertEqual(error.exception.code, "group_only_split")
        self.assertEqual(error.exception.details["valuations"], ["vg"])


class ReconciliationTest(unittest.TestCase):
    def test_donor_views_keep_agreement_boundaries(self):
        ledger = fresh_ledger()
        ledger.custody_receipt("gift-18", ["sheet-51b"], 5)
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        ledger.accession("gift-17", None, 8)
        ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 8)
        ledger.accession("gift-22", None, 10)
        ledger.issue_receipt("gift-22", ["sheet-77"], 10)
        ledger.correct_receipt("gift-ack-1", 10, "违规开具", "receipt-1")

        donor7 = ledger.reconciliation("a-7")
        self.assertEqual(
            {item["piece"] for item in donor7["received_by_museum"]},
            {"plate-51", "sheet-51a", "sheet-77"},
        )
        self.assertEqual(donor7["effective_receipt_total"], 86400 + 25200)
        self.assertEqual(donor7["outstanding_correction_total"], -43200)
        self.assertEqual(donor7["pending"], [])

        donor9 = ledger.reconciliation("a-9")
        # a-9 的 custody 主张在冲突中被裁决给 a-7 的 ownership
        self.assertEqual(donor9["received_by_museum"], [])
        self.assertEqual(
            donor9["resolved_under_other_agreement"],
            [{"piece": "sheet-51a", "own_agreement": "gift-18",
              "accessioned_under": "gift-17"}],
        )
        self.assertEqual(
            {item["piece"] for item in donor9["returned"]}, {"sheet-51b"})
        self.assertEqual(donor9["effective_receipt_total"], 0)

    def test_unknown_donor_rejected(self):
        with self.assertRaises(DomainError) as error:
            fresh_ledger().reconciliation("nobody")
        self.assertEqual(error.exception.code, "unknown_donor")


class ReplayTest(unittest.TestCase):
    def test_event_replay_rebuilds_identical_views(self):
        ledger = fresh_ledger()
        ledger.custody_receipt("gift-18", ["sheet-51b"], 5)
        ledger.resolve_conflict("sheet-51a", "gift-17", 8, "ok")
        ledger.accession("gift-17", None, 8)
        ledger.issue_receipt("gift-17", ["plate-51", "sheet-51a"], 8)
        replayed = Ledger(load_bundle(), [dict(event) for event in ledger.events])
        self.assertEqual(
            replayed.list_pieces(8), ledger.list_pieces(8))
        self.assertEqual(
            replayed.list_receipts(), ledger.list_receipts())
        self.assertEqual(replayed.events, ledger.events)


if __name__ == "__main__":
    unittest.main()
