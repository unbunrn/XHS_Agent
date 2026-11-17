import unittest

from bot.services.approvals import _parse_command
from tests.helpers import cleanup, new_env


class ApprovalParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = new_env("approval_parser")

    def tearDown(self) -> None:
        cleanup(self.env)

    def test_approval_with_space_is_parsed(self) -> None:
        self.assertEqual(_parse_command("批准 XH-UYV444"), ("approve", "XH-UYV444", ""))

    def test_approval_without_space_is_parsed(self) -> None:
        self.assertEqual(_parse_command("批准XH-UYV444"), ("approve", "XH-UYV444", ""))

    def test_reject_without_space_is_parsed(self) -> None:
        self.assertEqual(_parse_command("拒绝XH-UYV444"), ("reject", "XH-UYV444", ""))

    def test_revision_without_space_keeps_comment(self) -> None:
        self.assertEqual(
            _parse_command("修改XH-UYV444 标题再短一点"),
            ("revise", "XH-UYV444", "标题再短一点"),
        )

    def test_repeated_action_word_is_still_an_approval(self) -> None:
        self.assertEqual(
            _parse_command("批准批准XH-UYV444"),
            ("approve", "XH-UYV444", ""),
        )
        self.assertEqual(
            _parse_command("批准 批准 XH-UYV444"),
            ("approve", "XH-UYV444", ""),
        )

