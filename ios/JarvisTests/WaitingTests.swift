import XCTest
@testable import Jarvis

/// `GET /inherent/waiting` as the brain writes it (jarvis/surface/inherent_server.py `waiting`, claude_hooks.py, plugin_connections.py), and the answer each button makes.
final class WaitingTests: XCTestCase {
    private func cards(_ json: String) -> [WaitingCard] { WaitingCards.parse(Data(json.utf8)) ?? [] }
    private func body(_ r: (path: String, body: [String: Any])?) -> NSDictionary { (r?.body ?? [:]) as NSDictionary }

    func testNothingWaitingIsNoCards() {
        XCTAssertEqual(cards(#"{"confirmation": null, "clarification": null, "notices": {"notices": [], "audio_private": false, "hold": null, "departure": null}, "plugin_request": null, "claude_requests": []}"#), [])
        XCTAssertNil(WaitingCards.parse(Data("[]".utf8)), "not an answer of the route")
    }

    func testAMailDraftIsSentOrCancelledOnTheConfirmationRoute() throws {
        let c = try XCTUnwrap(cards(#"{"confirmation": {"id": "c1", "tool": "mcp__gmail__gmail_send", "action": "发这封邮件", "source": "gmail", "letter": true, "args": {"to": ["rachel@x.com", "li@x.com"], "subject": "谢谢", "body": "Hi Rachel", "content_sha256": "ab"}}}"#).first)
        XCTAssertEqual(c.kind, .confirmation)
        XCTAssertEqual(c.title, "发这封邮件？")
        XCTAssertEqual(c.meta, "给 rachel@x.com, li@x.com · 谢谢")
        XCTAssertEqual(c.body, "Hi Rachel")
        XCTAssertEqual(c.actions.map(\.label), ["取消", "发送"])
        let send = c.actions[1].answer.request(for: c)
        XCTAssertEqual(send?.path, "/inherent/confirmation")
        XCTAssertEqual(body(send), ["confirmation_id": "c1", "decision": "accept"])
        XCTAssertEqual(body(c.actions[0].answer.request(for: c)), ["confirmation_id": "c1", "decision": "reject"])
    }

    func testAnyOtherConfirmationListsItsArgumentsAndAsksAllow() throws {
        let c = try XCTUnwrap(cards(#"{"confirmation": {"id": "c2", "tool": "spawn_worker", "action": "派这个后台任务", "source": "", "letter": false, "args": {"goal": "整理笔记", "content_sha256": "ab", "n": 3, "nested": {"a": 1}}}}"#).first)
        XCTAssertEqual(c.actions.map(\.label), ["拒绝", "允许"])
        XCTAssertEqual(c.body, "goal: 整理笔记\nn: 3", "the content's hash and a nested value are not shown")
    }

    func testAnAskCardAnswersWithTheFilledFieldsOrIsDismissed() throws {
        let c = try XCTUnwrap(cards(#"{"clarification": {"id": "q1", "question": "送到哪里？", "fields": [{"label": "地址"}, {"label": "时间", "choices": ["现在", "晚点"], "value": "现在"}]}}"#).first)
        XCTAssertEqual(c.title, "送到哪里？")
        XCTAssertEqual(c.fields, [CardField(label: "地址", choices: []), CardField(label: "时间", choices: ["现在", "晚点"])])
        let fill = c.actions.last!.answer.request(for: c, filled: ["地址": " 主街 1 号 ", "时间": "  "])
        XCTAssertEqual(fill?.path, "/inherent/clarification")
        XCTAssertEqual(body(fill), ["clarification_id": "q1", "answers": ["地址": " 主街 1 号 "]], "an empty field is left out; the brain does the trimming")
        XCTAssertEqual(body(c.actions[0].answer.request(for: c)), ["clarification_id": "q1", "dismiss": true])
    }

    func testAHeldClaudePromptOffersAlwaysOnlyWhenItHasOne() throws {
        let both = cards(#"{"claude_requests": [{"id": "r1", "tool": "Bash", "input": {"command": "npm test"}, "cwd": "/Users/a/Projects/jarvis", "always": "Don't ask again for Bash(npm test)", "session_id": "s", "title": "修测试", "project": "jarvis"}, {"id": "r2", "tool": "Write", "input": {"file_path": "/tmp/a.txt", "content": "x"}, "cwd": "/tmp", "always": "", "session_id": "s2", "title": "", "project": ""}]}"#)
        XCTAssertEqual(both.map(\.title), ["Claude Code 想用 Bash", "Claude Code 想用 Write"])
        XCTAssertEqual(both[0].actions.map(\.label), ["拒绝", "一直允许", "允许"])
        XCTAssertEqual(both[1].actions.map(\.label), ["拒绝", "允许"])
        XCTAssertEqual(both[0].meta, "jarvis · 修测试")
        XCTAssertEqual(both[1].meta, "tmp", "no project: the last part of the folder")
        XCTAssertEqual(both[0].body, "npm test")
        XCTAssertEqual(both[0].hint, "Don't ask again for Bash(npm test)")
        XCTAssertEqual(both[1].body, "/tmp/a.txt")
        let always = both[0].actions[1].answer.request(for: both[0])
        XCTAssertEqual(always?.path, "/inherent/claude-requests/r1")
        XCTAssertEqual(body(always), ["decision": "always"])
        XCTAssertEqual(body(both[1].actions[0].answer.request(for: both[1])), ["decision": "deny"])
    }

    func testAClaudeQuestionCannotBeAllowedFromThePhone() throws {
        let c = try XCTUnwrap(cards(#"{"claude_requests": [{"id": "r3", "tool": "AskUserQuestion", "input": {"questions": [{"question": "用哪个库？"}]}, "cwd": "/x", "always": "", "session_id": "s", "title": "", "project": ""}]}"#).first)
        XCTAssertEqual(c.actions.map(\.label), ["拒绝"])
        XCTAssertTrue(c.body.contains("用哪个库？"))
    }

    func testANoticeIsDismissed() throws {
        let c = try XCTUnwrap(cards(#"{"notices": {"notices": [{"id": "rem1", "kind": "mail", "title": "给 Mia 回电话", "line": "晚了 5 分钟", "level": "card", "text": "给 Mia 回电话", "mail_kind": "reminder"}], "audio_private": false, "hold": null, "departure": null}}"#).first)
        XCTAssertEqual(c.short, "提醒")
        XCTAssertEqual(c.actions.map(\.label), ["知道了"])
        let r = c.actions[0].answer.request(for: c)
        XCTAssertEqual(r?.path, "/inherent/notices/rem1")
        XCTAssertEqual(body(r), ["action": "dismissed"])
    }

    func testAPluginSignInGoesThroughItsStates() throws {
        func card(_ state: String, extra: String = "") -> WaitingCard? {
            cards(#"{"plugin_request": {"id": "p1", "plugin_id": "notion", "purpose": "", "state": "\#(state)", "error": null\#(extra)}}"#).first
        }
        let offered = try XCTUnwrap(card("offered"))
        XCTAssertEqual(offered.actions.map(\.label), ["取消", "连接"])
        let connect = offered.actions[1].answer.request(for: offered)
        XCTAssertEqual(connect?.path, "/inherent/plugins/action")
        XCTAssertEqual(body(connect), ["operation": "connect", "data": ["request_id": "p1"]])
        XCTAssertEqual(try XCTUnwrap(card("connecting")).actions.map(\.label), ["取消"])
        let login = try XCTUnwrap(card("authorizing", extra: #", "auth_url": "https://auth.example.com/x""#))
        XCTAssertEqual(login.loginURL, URL(string: "https://auth.example.com/x"))
        XCTAssertEqual(login.actions.map(\.label), ["取消", "登录"])
        XCTAssertNil(login.actions[1].answer.request(for: login), "the link is opened on the phone, nothing is sent")
        XCTAssertNil(card("ready"))
        XCTAssertNil(card("cancelled"))
    }

    func testOneCardThatCannotBeReadCostsOnlyThatCard() {
        let list = cards(#"{"confirmation": {"tool": "x"}, "claude_requests": [{"id": "r1", "tool": "Bash", "input": {}, "cwd": "/a", "always": ""}], "notices": 5}"#)
        XCTAssertEqual(list.map(\.kind), [.claude])
    }
}
