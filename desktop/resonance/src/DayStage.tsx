import type { L } from './companionSettings';
import type { DayModel } from './dayView';
import type { DoorCtx } from './doors';

// The right column of 今天 while no row is selected (DayTimeline.tsx): 现在 / 等你 / 接下来. The frame (DayTimeline) gives it the column and
// calls it with the day it has built; what it shows of the day is read from `model` alone. Nothing here owns state the frame needs.
export type DayStageProps = {
  model: DayModel;                   // the day as drawn: rows, waiting keys, segments, blocks (dayView.buildDay)
  now: number;                       // ms since epoch, the minute the pane is drawn for (the model's own `now` is the same minute on today)
  t: (l: L) => string;               // the pane's words table: t(['Waiting', '等你'])
  onPick: (key: string) => void;     // select a row of the day: its detail replaces this stage, and the list and the bar follow
  sessions?: DoorCtx['sessions'];    // the companion's sessions, for the agents that are asking him something
};

// The frame's slot: the real stage replaces the body of this component.
export function DayStage(_: DayStageProps) {
  return <div className="dt-stage-home"/>;
}
