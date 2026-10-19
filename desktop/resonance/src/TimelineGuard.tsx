import { Component, type ReactNode } from 'react';
import { report } from './dayView';

// The day comes from a brain that keeps changing. A day the pane cannot draw must cost the pane and nothing else: an error thrown in a
// render unmounts the whole window, her included. The error is said once in the log (`[timeline] ...`, which electron/companion.ts
// passes on) and `fallback` takes the place; a new day to draw (`reset` changed) is tried again.
type Props = { where: string; fallback: ReactNode; reset?: unknown; children?: ReactNode };
export class TimelineGuard extends Component<Props, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { report(this.props.where, error); }
  componentDidUpdate(prev: Props) { if (this.state.failed && prev.reset !== this.props.reset) this.setState({ failed: false }); }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
