"""请求体构造：**图片怎么进上游**（2026-10-12 接线）。

盯的是这个 bug 的形状——它以前是**静默**的：
    发图给模型 → 模型回「您没有提供具体的图片」→ HTTP 却是 200、用量照报。
根因两处：`_text_of` 把 content 块数组拍平成纯文本（非 text 段直接丢），
`model_config.is_vl` 写死 False。

所以这里的断言分两层：
  ① **块数组别拍平**：user 消息的 `contents` 里文本与图并排；
  ② **`is_vl` 只在真有图时开**：它是模型选择/路由的输入之一，无谓翻转等于每次
     纯文本请求都换一个模型跑（本仓库最贵的教训就是「模型换了自己不说」）。

跑法（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qoder
from qoder import Credentials

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def cred() -> Credentials:
    return Credentials(uid="u", name="n", user_type="trial", machine_id="m",
                       token="dt-x", refresh_token="drt-x", expires_at=0)


def img_block():
    return {"type": "image_url", "image_url": {"url": PNG}}


class ImagesOfTest(unittest.TestCase):
    def test_picks_image_url_blocks(self):
        self.assertEqual(qoder._images_of([{"type": "text", "text": "问"}, img_block()]),
                         [img_block()])

    def test_plain_string_and_text_only_yield_nothing(self):
        self.assertEqual(qoder._images_of("纯字符串"), [])
        self.assertEqual(qoder._images_of([{"type": "text", "text": "问"}]), [])

    def test_malformed_image_block_is_skipped_not_raised(self):
        # 形状不对就跳过：报错会让整条请求 500，而"少一张图"至少还答得出别的
        self.assertEqual(qoder._images_of([{"type": "image_url"}]), [])
        self.assertEqual(qoder._images_of([{"type": "image_url", "image_url": "裸串"}]), [])


class UserMessageTest(unittest.TestCase):
    def test_image_and_text_sit_side_by_side_in_contents(self):
        m = qoder._convert_message(
            {"role": "user", "content": [{"type": "text", "text": "问"}, img_block()]})
        self.assertEqual(m["contents"], [{"type": "text", "text": "问"}, img_block()])

    def test_image_only_message_is_not_dropped(self):
        # 原先 `if not text.strip(): return None` 会把「只有图没字」整条丢掉
        m = qoder._convert_message({"role": "user", "content": [img_block()]})
        self.assertIsNotNone(m)
        self.assertEqual(m["contents"], [{"type": "text", "text": ""}, img_block()])

    def test_empty_message_still_dropped(self):
        self.assertIsNone(qoder._convert_message({"role": "user", "content": ""}))


class IsVlTest(unittest.TestCase):
    def body(self, content):
        return qoder.build_body(cred(), [{"role": "user", "content": content}], None,
                                "qfmodel", "none")

    def test_is_vl_off_for_text_only(self):
        self.assertIs(self.body("只回答两个字：收到")["model_config"]["is_vl"], False)
        self.assertIs(self.body([{"type": "text", "text": "问"}])["model_config"]["is_vl"], False)

    def test_is_vl_on_when_an_image_is_present(self):
        b = self.body([{"type": "text", "text": "问"}, img_block()])
        self.assertIs(b["model_config"]["is_vl"], True)

    def test_text_part_is_unchanged_for_plain_requests(self):
        # 回归闸：纯字符串那条老路必须逐字节不变（上下文缓存靠它）
        b = self.body("只回答两个字：收到")
        self.assertEqual(b["messages"][-1]["contents"], [{"type": "text", "text": "只回答两个字：收到"}])

    def test_imageUrls_fields_stay_empty(self):
        # 实测它们**不用填**（图走 contents 就够了）；填了反而是在猜形状，
        # 而"猜形状"正是 docs/REVERSE_ENGINEERING.md §3.1 那条死路。
        b = self.body([{"type": "text", "text": "问"}, img_block()])
        self.assertIsNone(b["chat_context"]["imageUrls"])
        self.assertIsNone(b["image_urls"])


if __name__ == "__main__":
    unittest.main()
