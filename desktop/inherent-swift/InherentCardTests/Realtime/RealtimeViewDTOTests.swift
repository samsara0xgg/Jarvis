import XCTest
@testable import InherentRealtime

/// The D10 message catalog and D8 snapshot frames, decoded from the shapes the
/// server sends.  Inline JSON rather than golden files: these DTOs have no
/// Python fixture yet, and the point of each case is the wire spelling.
final class RealtimeViewDTOTests: XCTestCase {
  private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try JSONDecoder().decode(type, from: Data(json.utf8))
  }

  // MARK: - Durable mutations

  func test_viewDeltaDecodesEveryDurableMutation() throws {
    let json = """
      {
        "source_event_uid": "E-0001",
        "changes": [
          {"kind": "response.opened", "response_id": "RESP-1",
           "response_group_id": "G-1", "turn_id": "T-1", "phase": "final",
           "channel": "both", "lifecycle": "generating", "question": "what time",
           "summary": null, "created_at_ms": 1788200000000, "revision": 1},
          {"kind": "response.segment", "response_id": "RESP-1", "sequence": 0,
           "phase": "final", "channel": "document", "text": "hello",
           "segment_hash": "h0"},
          {"kind": "response.delivery", "response_id": "RESP-1",
           "panel_stream": "open", "reason": null},
          {"kind": "response.lifecycle", "response_id": "RESP-1",
           "lifecycle": "waiting_action", "terminal_reason": null, "revision": 2},
          {"kind": "playback.state", "response_id": "RESP-1",
           "playback_generation_id": "P-1", "phase": "interrupted",
           "heard_through_sequence": 0, "revision": 3},
          {"kind": "action.upsert", "action_id": "A-1", "response_group_id": "G-1",
           "task_id": "TASK-1", "state": "running", "label": "turn on the light",
           "target": "kitchen", "revision": 4, "cancellable": true,
           "freshness_ms": 250},
          {"kind": "confirmation.upsert", "confirmation_id": "C-1",
           "response_group_id": "G-1", "action_id": "A-1", "summary": "unlock",
           "target": "front door", "risk": "high", "options": ["accept", "reject"],
           "expires_at_ms": 1788200030000, "revision": 5},
          {"kind": "confirmation.cleared", "confirmation_id": "C-1",
           "reason": "superseded", "revision": 6},
          {"kind": "input.committed", "utterance_id": "U-1", "turn_id": "T-1",
           "response_group_id": "G-1", "source": "voice_ptt", "text": "what time"},
          {"kind": "surface.notice", "notice_id": "N-1", "subject_ref": "RESP-1",
           "severity": "warning", "code": "tts_unavailable", "text": "no audio",
           "required_action": "retry"}
        ]
      }
      """
    let delta = try decode(ViewDeltaPayload.self, json)
    XCTAssertEqual(delta.sourceEventUid, "E-0001")
    XCTAssertEqual(delta.changes.count, 10)

    guard case .responseOpened(let opened) = delta.changes[0] else {
      return XCTFail("expected response.opened")
    }
    XCTAssertEqual(opened.responseId, "RESP-1")
    XCTAssertEqual(opened.responseGroupId, "G-1")
    XCTAssertEqual(opened.turnId, "T-1")
    XCTAssertEqual(opened.phase, .final)
    XCTAssertEqual(opened.channel, .both)
    XCTAssertEqual(opened.lifecycle, .generating)
    XCTAssertEqual(opened.question, "what time")
    XCTAssertNil(opened.summary)
    XCTAssertEqual(opened.createdAtMs, 1_788_200_000_000)
    XCTAssertEqual(opened.revision, 1)

    guard case .responseSegment(let segment) = delta.changes[1] else {
      return XCTFail("expected response.segment")
    }
    XCTAssertEqual(segment.sequence, 0)
    XCTAssertEqual(segment.text, "hello")
    XCTAssertEqual(segment.segmentHash, "h0")

    guard case .responseDelivery(let delivery) = delta.changes[2] else {
      return XCTFail("expected response.delivery")
    }
    XCTAssertEqual(delivery.panelStream, .open)

    guard case .responseLifecycle(let lifecycle) = delta.changes[3] else {
      return XCTFail("expected response.lifecycle")
    }
    XCTAssertEqual(lifecycle.lifecycle, .waitingAction)
    XCTAssertFalse(lifecycle.lifecycle.isTerminal)

    guard case .playbackState(let playback) = delta.changes[4] else {
      return XCTFail("expected playback.state")
    }
    XCTAssertEqual(playback.playbackGenerationId, "P-1")
    XCTAssertEqual(playback.phase, .interrupted)
    XCTAssertEqual(playback.heardThroughSequence, 0)

    guard case .actionUpsert(let action) = delta.changes[5] else {
      return XCTFail("expected action.upsert")
    }
    XCTAssertEqual(action.state, .running)
    XCTAssertEqual(action.taskId, "TASK-1")
    XCTAssertTrue(action.cancellable)
    XCTAssertEqual(action.freshnessMs, 250)

    guard case .confirmationUpsert(let confirmation) = delta.changes[6] else {
      return XCTFail("expected confirmation.upsert")
    }
    XCTAssertEqual(confirmation.options, ["accept", "reject"])
    XCTAssertEqual(confirmation.expiresAtMs, 1_788_200_030_000)

    guard case .confirmationCleared(let cleared) = delta.changes[7] else {
      return XCTFail("expected confirmation.cleared")
    }
    XCTAssertEqual(cleared.reason, .superseded)

    guard case .inputCommitted(let committed) = delta.changes[8] else {
      return XCTFail("expected input.committed")
    }
    XCTAssertEqual(committed.source, .voicePtt)
    XCTAssertEqual(committed.utteranceId, "U-1")

    guard case .surfaceNotice(let notice) = delta.changes[9] else {
      return XCTFail("expected surface.notice")
    }
    XCTAssertEqual(notice.severity, .warning)
    XCTAssertEqual(notice.code, "tts_unavailable")
    XCTAssertEqual(notice.requiredAction, "retry")
  }

  func test_unknownDurableMutationKindThrows() throws {
    let json = """
      {"source_event_uid": "E-0002",
       "changes": [{"kind": "response.teleported", "response_id": "RESP-1"}]}
      """
    XCTAssertThrowsError(try decode(ViewDeltaPayload.self, json)) { error in
      XCTAssertEqual(
        error as? RealtimeProtocolError,
        .invalidField(field: "kind", reason: "unknown durable mutation response.teleported")
      )
    }
  }

  func test_negativeSegmentSequenceIsRejected() throws {
    let json = """
      {"kind": "response.segment", "response_id": "RESP-1", "sequence": -1,
       "phase": "final", "channel": "document", "text": "x", "segment_hash": "h"}
      """
    XCTAssertThrowsError(try decode(ViewMutation.self, json)) { error in
      XCTAssertEqual(
        error as? RealtimeProtocolError,
        .invalidField(field: "sequence", reason: "must be >= 0")
      )
    }
  }

  // MARK: - Ephemeral updates

  func test_ephemeralUpdatesDecodeByKind() throws {
    guard case .inputState(let state) = try decode(
      EphemeralUpdate.self,
      #"{"kind": "input.state", "utterance_id": "U-1", "revision": 3, "state": "transcribing"}"#
    ) else { return XCTFail("expected input.state") }
    XCTAssertEqual(state.state, .transcribing)
    XCTAssertEqual(state.revision, 3)

    guard case .inputPartial(let partial) = try decode(
      EphemeralUpdate.self,
      #"{"kind": "input.partial", "utterance_id": "U-1", "revision": 4, "text": "what ti"}"#
    ) else { return XCTFail("expected input.partial") }
    XCTAssertEqual(partial.text, "what ti")

    guard case .playbackProgress(let progress) = try decode(
      EphemeralUpdate.self,
      """
      {"kind": "playback.progress", "response_id": "RESP-1",
       "playback_generation_id": "P-1", "state": "silenced",
       "heard_through_sequence": 2, "silence_at_monotonic_ns": 991,
       "cursor_quality": "conservative"}
      """
    ) else { return XCTFail("expected playback.progress") }
    XCTAssertEqual(progress.state, .silenced)
    XCTAssertEqual(progress.silenceAtMonotonicNs, 991)
    XCTAssertEqual(progress.cursorQuality, "conservative")

    guard case .actionProgressHint(let hint) = try decode(
      EphemeralUpdate.self,
      #"{"kind": "action.progress_hint", "action_id": "A-1", "stage": "dispatching", "text": null}"#
    ) else { return XCTFail("expected action.progress_hint") }
    XCTAssertEqual(hint.stage, "dispatching")
    XCTAssertNil(hint.text)

    guard case .connectionNotice(let notice) = try decode(
      EphemeralUpdate.self,
      #"{"kind": "connection.notice", "code": "degraded", "text": "slow link"}"#
    ) else { return XCTFail("expected connection.notice") }
    XCTAssertEqual(notice.code, "degraded")

    guard case .clear(let clear) = try decode(
      EphemeralUpdate.self,
      #"{"kind": "ephemeral.clear", "key": "input.partial:U-1"}"#
    ) else { return XCTFail("expected ephemeral.clear") }
    XCTAssertEqual(clear.key, "input.partial:U-1")
  }

  func test_ephemeralBaselineDecodesItemsByItemMessageType() throws {
    let json = """
      {
        "kind": "ephemeral.baseline",
        "watermark_sequence": 42,
        "items": [
          {"key": "input.partial:U-1", "item_message_type": "input.partial",
           "item_sequence": 40,
           "typed_payload": {"utterance_id": "U-1", "revision": 2, "text": "wh"}},
          {"key": "playback.progress:RESP-1", "item_message_type": "playback.progress",
           "item_sequence": 41,
           "typed_payload": {"response_id": "RESP-1", "playback_generation_id": "P-1",
                             "state": "playing"}}
        ]
      }
      """
    guard case .baseline(let baseline) = try decode(EphemeralUpdate.self, json) else {
      return XCTFail("expected ephemeral.baseline")
    }
    XCTAssertEqual(baseline.watermarkSequence, 42)
    XCTAssertEqual(baseline.items.count, 2)
    guard case .inputPartial(let partial) = baseline.items[0].typedPayload else {
      return XCTFail("expected a typed input.partial item")
    }
    XCTAssertEqual(partial.text, "wh")
    guard case .playbackProgress(let progress) = baseline.items[1].typedPayload else {
      return XCTFail("expected a typed playback.progress item")
    }
    XCTAssertEqual(progress.state, .playing)
    XCTAssertNil(progress.heardThroughSequence)
  }

  func test_unknownEphemeralKindDecodesToUnknown() throws {
    let update = try decode(
      EphemeralUpdate.self,
      #"{"kind": "playback.aura", "response_id": "RESP-1"}"#
    )
    XCTAssertEqual(update, .unknown(kind: "playback.aura"))
  }

  // MARK: - Snapshot frames

  func test_snapshotBeginAndEndDecode() throws {
    let begin = try decode(
      SnapshotBegin.self,
      """
      {"snapshot_id": "S-1", "through_cursor": 900, "view_schema_version": 1,
       "section_order": ["response_groups", "actions", "pending_confirmation",
                         "capabilities", "surface_notices"],
       "counts": {"response_groups": 1, "actions": 1}}
      """
    )
    XCTAssertEqual(begin.snapshotId, "S-1")
    XCTAssertEqual(begin.throughCursor, 900)
    XCTAssertEqual(begin.viewSchemaVersion, 1)
    XCTAssertEqual(
      begin.sectionOrder,
      [.responseGroups, .actions, .pendingConfirmation, .capabilities, .surfaceNotices]
    )
    XCTAssertEqual(begin.counts["response_groups"], 1)

    let end = try decode(
      SnapshotEnd.self,
      #"{"snapshot_id": "S-1", "through_cursor": 900, "content_hash": "abc123"}"#
    )
    XCTAssertEqual(end.contentHash, "abc123")
    XCTAssertEqual(end.throughCursor, begin.throughCursor)
  }

  func test_snapshotPageItemsDecodeBySection() throws {
    let groups = try decode(
      SnapshotPage.self,
      """
      {"snapshot_id": "S-1", "section": "response_groups", "page_index": 0,
       "items": [{"response_group_id": "G-1", "turn_id": "T-1", "question": "what time",
                  "created_at_ms": 1788200000000,
                  "responses": [{"response_id": "RESP-1", "phase": "final",
                                 "channel": "both", "lifecycle": "completed",
                                 "revision": 7, "panel_stream": "closed",
                                 "segments": [{"response_id": "RESP-1", "sequence": 0,
                                               "phase": "final", "channel": "document",
                                               "text": "it is noon",
                                               "segment_hash": "h0"}],
                                 "playbacks": [{"playback_generation_id": "P-1",
                                                "phase": "completed",
                                                "heard_through_sequence": 0,
                                                "revision": 7}]}]}]}
      """
    )
    guard case .responseGroups(let items) = groups.items else {
      return XCTFail("expected response_groups items")
    }
    XCTAssertEqual(groups.section, .responseGroups)
    XCTAssertEqual(items.count, 1)
    XCTAssertEqual(items[0].responses[0].segments[0].text, "it is noon")
    XCTAssertEqual(items[0].responses[0].playbacks[0].phase, .completed)

    let actions = try decode(
      SnapshotPage.self,
      """
      {"snapshot_id": "S-1", "section": "actions", "page_index": 0,
       "items": [{"action_id": "A-1", "response_group_id": "G-1", "task_id": null,
                  "state": "result_observed", "label": "light", "target": null,
                  "revision": 9, "cancellable": false, "freshness_ms": null}]}
      """
    )
    guard case .actions(let actionItems) = actions.items else {
      return XCTFail("expected actions items")
    }
    XCTAssertEqual(actionItems[0].state, .resultObserved)
    XCTAssertTrue(actionItems[0].state.isTerminal)

    let confirmation = try decode(
      SnapshotPage.self,
      """
      {"snapshot_id": "S-1", "section": "pending_confirmation", "page_index": 0,
       "items": [{"confirmation_id": "C-1", "response_group_id": "G-1",
                  "action_id": "A-1", "summary": "unlock", "target": "door",
                  "risk": "high", "options": ["accept"], "expires_at_ms": 1,
                  "revision": 2}]}
      """
    )
    guard case .pendingConfirmation(let confirmationItems) = confirmation.items else {
      return XCTFail("expected pending_confirmation items")
    }
    XCTAssertEqual(confirmationItems.count, 1)
    XCTAssertEqual(confirmationItems[0].confirmationId, "C-1")

    let capabilities = try decode(
      SnapshotPage.self,
      """
      {"snapshot_id": "S-1", "section": "capabilities", "page_index": 0,
       "items": [{"runtime_capabilities": {"text_input": true, "image_input": false,
                                           "voice_input": true, "response_interrupt": true,
                                           "action_cancel": true,
                                           "confirmation_actions": true,
                                           "natural_barge_in": false,
                                           "aec_profile": "mac_builtin"},
                  "projection_freshness_ms": 12}]}
      """
    )
    guard case .capabilities(let capabilityItems) = capabilities.items else {
      return XCTFail("expected capabilities items")
    }
    XCTAssertTrue(capabilityItems[0].runtimeCapabilities.voiceInput)
    XCTAssertEqual(capabilityItems[0].projectionFreshnessMs, 12)

    let notices = try decode(
      SnapshotPage.self,
      """
      {"snapshot_id": "S-1", "section": "surface_notices", "page_index": 1,
       "items": [{"notice_id": "N-1", "subject_ref": null, "severity": "error",
                  "code": "mic_denied", "text": null, "required_action": "grant"}]}
      """
    )
    guard case .surfaceNotices(let noticeItems) = notices.items else {
      return XCTFail("expected surface_notices items")
    }
    XCTAssertEqual(notices.pageIndex, 1)
    XCTAssertEqual(noticeItems[0].severity, .error)
  }
}
