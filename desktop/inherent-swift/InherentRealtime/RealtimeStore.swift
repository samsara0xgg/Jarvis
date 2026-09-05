import Combine
import Foundation

/// The ADR-0014 D4 store: the sole `@MainActor ObservableObject` and the sole
/// entry through which `InherentUXState` changes.
///
/// It owns no logic of its own.  `apply` folds one event with the pure reducer
/// and hands the effects the reducer earned to the runner, so every mutation is
/// a reducer decision and every side effect happens outside it.  There is no
/// second setter: views observe `state`, and the transport actor is the only
/// caller that feeds it.
@MainActor
public final class RealtimeStore: ObservableObject {
  @Published public private(set) var state: InherentUXState

  /// The D19 local truth, exposed for the views that bind to it.
  public var presentation: LocalPresentationState { state.presentation }

  private let runner: any RealtimeEffectRunner
  private let clock: ContinuousClock

  public init(
    initial: InherentUXState = InherentUXState(),
    runner: any RealtimeEffectRunner,
    clock: ContinuousClock = ContinuousClock()
  ) {
    self.state = initial
    self.runner = runner
    self.clock = clock
  }

  /// Folds one event, then performs its effects.  The `await` is what lets the
  /// transport hold its mailbox until this apply is complete (D4).
  public func apply(_ event: InherentClientEvent) async {
    let effects = InherentReducer.reduce(state: &state, event: event, now: clock.now)
    await runner.run(effects)
  }
}
