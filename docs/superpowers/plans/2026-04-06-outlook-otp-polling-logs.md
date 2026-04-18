# Outlook OTP Polling Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Outlook 本地池的 OTP 收码链路补充精简诊断日志，并增加 `INBOX` / `Junk` 双文件夹轮询，便于定位“等待验证码”阶段的真实阻塞点。

**Architecture:** 保持 ChatGPT 主流程不变，只在 `OutlookMailbox.wait_for_code()` 内增加诊断能力。实现上按每轮 poll 依次检查 `INBOX` 与 `Junk`，记录 IMAP 连接、UID 数量、新邮件数量、命中 subject、验证码提取/跳过结果，以及异常原因；不打印正文，不改超时与轮询节奏。

**Tech Stack:** Python, unittest, imaplib-style doubles, existing `BaseMailbox` polling helpers

---

### Task 1: 为 Outlook OTP 轮询补充失败测试

**Files:**
- Create: `tests/test_outlook_mailbox.py`
- Test: `tests/test_outlook_mailbox.py`
- Verify against: `core/base_mailbox.py:3068-3405`

- [ ] **Step 1: 写出失败测试文件，覆盖 Junk 回退、异常日志、exclude 日志**

将 `tests/test_outlook_mailbox.py` 创建为以下内容：

```python
import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount, OutlookMailbox


class _FakeImapConnection:
    def __init__(self, folders=None):
        self.folders = folders or {}
        self.selected = []
        self.logged_out = False
        self.current_mailbox = None

    def select(self, mailbox, readonly=True):
        config = self.folders.get(mailbox, {})
        error = config.get("select_error")
        if error:
            raise error
        self.current_mailbox = mailbox
        self.selected.append((mailbox, readonly))
        return config.get("select_status", "OK"), [b""]

    def uid(self, command, *args):
        config = self.folders.get(self.current_mailbox, {})
        if command == "search":
            error = config.get("search_error")
            if error:
                raise error
            ids = config.get("ids", [])
            payload = b" ".join(
                uid if isinstance(uid, bytes) else str(uid).encode("utf-8")
                for uid in ids
            )
            return config.get("search_status", "OK"), [payload]
        if command == "fetch":
            error = config.get("fetch_error")
            if error:
                raise error
            uid = args[0]
            raw = config.get("messages", {}).get(uid)
            if raw is None:
                return "NO", []
            return "OK", [(b"RFC822", raw)]
        raise AssertionError(f"unexpected uid command: {command}")

    def logout(self):
        self.logged_out = True


class OutlookMailboxTests(unittest.TestCase):
    def _build_mailbox(self):
        mailbox = OutlookMailbox()
        self.logs = []
        mailbox._log_fn = self.logs.append
        return mailbox

    def _account(self):
        return MailboxAccount(
            email="demo@outlook.com",
            account_id="acc-1",
            extra={"password": "secret"},
        )

    def _raw_email(self, subject: str, body: str) -> bytes:
        return (
            f"Subject: {subject}\r\n"
            f"From: no-reply@example.com\r\n"
            f"To: demo@outlook.com\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}"
        ).encode("utf-8")

    def test_wait_for_code_logs_inbox_and_junk_and_returns_code_from_junk(self):
        mailbox = self._build_mailbox()
        mailbox._open_imap = mock.Mock(
            side_effect=[
                _FakeImapConnection({"INBOX": {"ids": []}}),
                _FakeImapConnection(
                    {
                        "Junk": {
                            "ids": [b"11"],
                            "messages": {
                                b"11": self._raw_email(
                                    "OpenAI verification code",
                                    "Your verification code is 222222",
                                )
                            },
                        }
                    }
                ),
            ]
        )

        code = mailbox.wait_for_code(self._account(), timeout=1)

        self.assertEqual(code, "222222")
        joined = "\n".join(self.logs)
        self.assertIn("[Outlook][OTP] folder=INBOX", joined)
        self.assertIn("[Outlook][OTP] folder=Junk", joined)
        self.assertIn("uid_total=0", joined)
        self.assertIn("new_uid_count=1", joined)
        self.assertIn("subject=OpenAI verification code", joined)
        self.assertIn("验证码提取成功: 222222", joined)

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("time.monotonic", side_effect=[0.0, 0.0, 0.2, 0.2, 0.4])
    def test_wait_for_code_logs_imap_exception_and_recovers_on_next_poll(self, _monotonic, _sleep):
        mailbox = self._build_mailbox()
        mailbox._open_imap = mock.Mock(
            side_effect=[
                RuntimeError("imap boom"),
                _FakeImapConnection(
                    {
                        "INBOX": {
                            "ids": [b"21"],
                            "messages": {
                                b"21": self._raw_email(
                                    "Security code",
                                    "Security code: 333333",
                                )
                            },
                        }
                    }
                ),
            ]
        )

        code = mailbox.wait_for_code(self._account(), timeout=2)

        self.assertEqual(code, "333333")
        joined = "\n".join(self.logs)
        self.assertIn("IMAP 查询异常: imap boom", joined)
        self.assertIn("subject=Security code", joined)
        self.assertIn("验证码提取成功: 333333", joined)

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("time.monotonic", side_effect=[0.0, 0.0, 0.2, 0.2, 0.4])
    def test_wait_for_code_logs_skipped_excluded_code_then_returns_next_code(self, _monotonic, _sleep):
        mailbox = self._build_mailbox()
        mailbox._open_imap = mock.Mock(
            side_effect=[
                _FakeImapConnection(
                    {
                        "INBOX": {
                            "ids": [b"31"],
                            "messages": {
                                b"31": self._raw_email(
                                    "Verification code",
                                    "Your verification code is 111111",
                                )
                            },
                        }
                    }
                ),
                _FakeImapConnection({"INBOX": {"ids": []}}),
                _FakeImapConnection(
                    {
                        "INBOX": {
                            "ids": [b"31", b"32"],
                            "messages": {
                                b"32": self._raw_email(
                                    "Verification code",
                                    "Your verification code is 222222",
                                )
                            },
                        }
                    }
                ),
            ]
        )

        code = mailbox.wait_for_code(
            self._account(),
            timeout=2,
            exclude_codes={"111111"},
        )

        self.assertEqual(code, "222222")
        joined = "\n".join(self.logs)
        self.assertIn("跳过已尝试验证码: 111111", joined)
        self.assertIn("验证码提取成功: 222222", joined)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行新测试，确认它先失败**

Run:

```bash
python -m unittest discover -s tests -p "test_outlook_mailbox.py" -v
```

Expected:
- FAIL
- 失败原因应包含以下一种或多种：
  - 还没有 `folder=Junk` 日志
  - 还没有 `IMAP 查询异常` 日志
  - 还没有 `跳过已尝试验证码` 日志
  - 当前实现不会从 `Junk` 返回验证码

- [ ] **Step 3: 确认失败与设计一致，而不是测试本身写错**

检查点：
- 当前 `OutlookMailbox.wait_for_code()` 只 `select("INBOX")`
- 当前实现没有 `_log(...)` 输出轮询统计
- 当前 `except Exception` 分支直接 `return None`

Expected: 可以明确说明测试失败是因为功能尚未实现，不是测试拼写或 mock 错误。

- [ ] **Step 4: 提交（仅在用户明确要求提交 git 时执行）**

```bash
git add tests/test_outlook_mailbox.py
git commit -m "test: add outlook otp polling diagnostics coverage"
```

### Task 2: 在 OutlookMailbox.wait_for_code 中实现诊断日志与 Junk 回退

**Files:**
- Modify: `core/base_mailbox.py:3332-3405`
- Test: `tests/test_outlook_mailbox.py`
- Regression: `tests/test_chatgpt_plugin.py`

- [ ] **Step 1: 读取当前 OutlookMailbox.wait_for_code 实现，锁定最小改动范围**

当前目标函数位于：
- `core/base_mailbox.py:3332-3405`

当前特征：
- 只查 `INBOX`
- 共享一组 `seen`
- 提取到验证码即返回
- 异常会被吞掉并返回 `None`

Expected: 本次只修改该函数，不改 `BaseMailbox._run_polling_wait()`，不改 ChatGPT 上层调用。

- [ ] **Step 2: 按设计改写 wait_for_code，加入精简日志与双文件夹轮询**

将 `OutlookMailbox.wait_for_code()` 改成如下实现：

```python
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from email import message_from_bytes
        from email.policy import default as email_default_policy

        seen = {f"INBOX:{mid}" for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()
        folders = ["INBOX", "Junk"]

        def poll_once() -> Optional[str]:
            for folder in folders:
                imap_conn = None
                try:
                    self._log(f"[Outlook][OTP] folder={folder} 开始轮询")
                    imap_conn = self._open_imap(account)
                    self._log(f"[Outlook][OTP] folder={folder} IMAP 登录成功")
                    status, _ = imap_conn.select(folder, readonly=True)
                    if status != "OK":
                        self._log(
                            f"[Outlook][OTP] folder={folder} select 失败: status={status}"
                        )
                        continue
                    status, data = imap_conn.uid("search", None, "ALL")
                    if status != "OK":
                        self._log(
                            f"[Outlook][OTP] folder={folder} search 失败: status={status}"
                        )
                        continue
                    ids = data[0].split() if data and data[0] else []
                    if len(ids) > 50:
                        ids = ids[-50:]
                    new_uids = []
                    for uid in ids:
                        uid_str = (
                            uid.decode("utf-8", errors="ignore")
                            if isinstance(uid, bytes)
                            else str(uid)
                        )
                        seen_key = f"{folder}:{uid_str}"
                        if not uid_str or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_uids.append(uid)
                    self._log(
                        f"[Outlook][OTP] folder={folder} uid_total={len(ids)} new_uid_count={len(new_uids)}"
                    )
                    for uid in new_uids:
                        status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                        if status != "OK":
                            self._log(
                                f"[Outlook][OTP] folder={folder} fetch 失败: uid={uid!r} status={status}"
                            )
                            continue
                        raw = None
                        for item in msg_data or []:
                            if isinstance(item, tuple) and item[1]:
                                raw = item[1]
                                break
                        if not raw:
                            self._log(
                                f"[Outlook][OTP] folder={folder} fetch 空响应: uid={uid!r}"
                            )
                            continue
                        msg = message_from_bytes(raw, policy=email_default_policy)
                        subject = self._decode_header_value(msg.get("Subject", ""))
                        text = self._extract_message_text(msg)
                        self._log(
                            f"[Outlook][OTP] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self._log(
                                f"[Outlook][OTP] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if not code:
                            self._log(
                                f"[Outlook][OTP] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self._log(
                                f"[Outlook][OTP] folder={folder} 跳过已尝试验证码: {code}"
                            )
                            continue
                        self._log(
                            f"[Outlook][OTP] folder={folder} 验证码提取成功: {code}"
                        )
                        return code
                except Exception as exc:
                    self._log(f"[Outlook][OTP] folder={folder} IMAP 查询异常: {exc}")
                    continue
                finally:
                    try:
                        if imap_conn:
                            imap_conn.logout()
                    except Exception:
                        pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )
```

实现要求：
- 保留 `timeout` / `poll_interval=5`
- 只打印统计、subject、提取结果、异常原因
- 不打印正文
- `seen` 必须加上 `folder:` 前缀，避免 `INBOX` 与 `Junk` 的 UID 冲突
- `before_ids` 继续只映射到 `INBOX`，保持和当前基线采样行为兼容

- [ ] **Step 3: 运行新测试，确认全部转绿**

Run:

```bash
python -m unittest discover -s tests -p "test_outlook_mailbox.py" -v
```

Expected:
- PASS
- 3 个测试全部通过

- [ ] **Step 4: 运行现有 ChatGPT custom_provider 回归测试**

Run:

```bash
python -m unittest discover -s tests -p "test_chatgpt_plugin.py" -v
```

Expected:
- PASS
- `test_custom_provider_uses_mailbox_baseline_for_verification_code`
- `test_custom_provider_prefers_configured_mailbox_timeout`
- `test_custom_provider_rejects_blank_email`

- [ ] **Step 5: 提交（仅在用户明确要求提交 git 时执行）**

```bash
git add core/base_mailbox.py tests/test_outlook_mailbox.py
git commit -m "fix: log outlook otp polling details"
```
