import json
import unittest

from app.browser.xhs_im import parse_conversations, parse_history


class XhsImParsingTests(unittest.TestCase):
    def test_conversation_payload_is_normalized(self):
        payload = {
            "code": 0,
            "success": True,
            "data": {"chats": [{
                "user_id": "self",
                "chat_user_id": "peer",
                "last_msg_time": 1_800_000_000_000,
                "last_msg_content": json.dumps({"content": "你好", "content_type": 1}),
                "max_store_id": 23,
                "start_store_id": 10,
                "info": {"nickname": "访客", "avatar": "https://img.test/a.jpg"},
            }]},
        }
        rows = parse_conversations(payload, {"data": {"peer": 3}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conv_id"], "peer")
        self.assertEqual(rows[0]["last_text"], "你好")
        self.assertEqual(rows[0]["unread_count"], 3)
        self.assertEqual(rows[0]["last_time"], 1_800_000_000)
        self.assertEqual(json.loads(rows[0]["raw_json"])["max_store_id"], 23)

    def test_history_payload_keeps_store_cursor_and_direction(self):
        payload = {"code": 0, "success": True, "data": {"out_message_list": [
            {"id": "m2", "store_id": 12, "sender_id": "self",
             "receiver_id": "peer", "created_at": 200,
             "content": json.dumps({"content": "已回复", "content_type": 1})},
            {"id": "m1", "store_id": 11, "sender_id": "peer",
             "receiver_id": "self", "created_at": 100,
             "content": json.dumps({"content": "咨询", "content_type": 1})},
        ]}}
        parsed = parse_history(payload, peer_uid="peer", self_uid="self")
        self.assertEqual([m["server_msg_id"] for m in parsed["messages"]], ["m1", "m2"])
        self.assertEqual([m["direction"] for m in parsed["messages"]], ["in", "out"])
        self.assertEqual(parsed["next_cursor"], 11)


if __name__ == "__main__":
    unittest.main()
