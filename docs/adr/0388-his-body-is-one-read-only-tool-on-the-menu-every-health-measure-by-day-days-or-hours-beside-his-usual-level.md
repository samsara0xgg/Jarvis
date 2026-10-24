# ADR 0388 — His body is one read-only tool on the menu: every Health measure, by day, days or hours, beside his usual level

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- Since the watch arrived (2026-10-18), the phone sends every Health sample as Health recorded it
  (ADR 0293), about 400 measurements a day. Heart rate comes about every four and a half minutes,
  asleep or awake, and Health marks about half of it as taken while he was still. The watch's
  charge comes through the phone too (ADR 0377).
- No tool of hers reads any of it:
  - every turn's ledger (ADR 0201) carries each day's sleep, steps and workout minutes;
  - `my_day` (ADR 0244) says the same three;
  - `GET /inherent/day` has steps, resting heart rate and HRV, but for the phone's page, not
    for her.
- So on 2026-10-23 at 17:36 he asked 「帮我看一下下午2点到现在心率是不是有点偏高，我是不是有点太激动了」.
  That afternoon's readings were in the log, and she said she could not see his heart rate.
- What he asks of his body, in his own words:
  - 「我睡咋样」 (2026-10-19 11:13), and 「我今晚睡咋样」 the next morning, meaning last night;
  - 「我这几天睡眠分析一下」 (2026-10-23 11:43);
  - the heart rate question above, which is about some hours of today against his normal.
- Whether a number is high is relative to him: the same hours on days before, and still apart
  from moving, since walking alone lifts heart rate.
- A stretch with no heart rate means something.
  - In his first five days, the median gap between readings was 4.5 minutes. A gap of 30 minutes
    or more came 0 to 4 times a day, 7 of 8 of them while he was awake.
  - The longest was 4 hours 10 minutes on 10-22 afternoon, while his steps went on: the watch
    was likely off his wrist. The log does not say why.
  - Heart rate reaches the brain up to about an hour after the watch takes it. The phone sends
    it at most hourly.
- Tool standard (docs/spec.html#tools):
  - one tool for one thing;
  - times in his words;
  - a speakable summary, at most about 4000 characters;
  - a receipt that says what was read (ADR 0319);
  - the time taken only from the call's clock (ADR 0322);
  - SQL only in L2's named questions (ADR 0349).
- On 2026-10-24 Allen asked for one general tool that can read everything here (「我准备直接做
  一个 general 的工具，让它什么都可以查」), done properly the first time, held to the best outside
  standards.

## Decision

Give her one read-only tool on the menu, `body`, that reads every Health measure the phone sends,
over a day, a span of days (at most 31) or some hours of one day, in his local time.

- **What comes back for each measure.**
  - A reading measure: count, low, median and high, with the readings Health took while he was
    still apart. They are said as "while still", never "at rest": tried on the brain model on
    2026-10-24, she said the "at rest" median (71) as his resting heart rate, which Health
    estimates on its own, once a day, and lower.
  - A total: its sum, as Health keeps it by the hour.
  - Sleep: the nights he woke from on the days asked for, each with its stages. A span of days
    names its days with no night: unnamed, she took the span's first day for the first night
    recorded.
  - Workouts: a list, with what Health knew of each.
  - Stretch rows come with a measure asked for alone or with one or two others.
  - No measure named: one line each, and the rest named as not recorded.
- **Beside his usual.**
  - Each measure stands beside its median and middle half over the same hours of the 14 days
    before the window. For a span of days it is those whole days.
  - For a total to now, the hour that is still filling counts by its part.
  - It says below, within or above. With fewer than 3 such days it says that, and gives no
    verdict.
- **What is missing is said.**
  - Every stretch of 30 minutes or more without heart rate is named, with the watch's charge
    readings inside it.
  - A window that runs to now says when its newest heart rate is, while readings may still be
    coming.
  - A measure with nothing recorded says when the last record before was.
- **Time.**
  - `when` is his words, read by the day resolver (ADR 0356).
  - Last night is the sleep he woke from today. For other measures, last night is refused with
    how to ask.
  - Hours are clock times of that one day. Nothing is in the future.
- It never judges what a number means; she does.

## Alternatives rejected

- **Add heart rate to `my_day`.** `my_day` reads days from the ledger, with no hours inside a day
  and no usual level. His question was 2 pm to now against his normal. 22 more measures there
  would push its answer past 4000 characters for a span of days.
- **One tool per measure (heart rate, sleep, steps).**
  - Every one would repeat the same window, usual level and gaps.
  - Asking about sleep and heart rate together would take two calls.
  - The standard asks for one tool for one thing.
- **Let the model write SQL over the event log.**
  - ADR 0349 keeps SQL in L2.
  - Raw rows would not drop a sample sent twice.
  - Raw rows would leave the usual level, the hours and the gaps for the model to work out each
    time.
- **Put the day's body figures in every turn's state.** Whatever the state carries goes in every
  prompt. Hours and a usual level differ with each question, so they cannot be worked out ahead.
- **Compare against Apple's population ranges.** Health does not send them. A range for everyone
  says nothing of whether this afternoon is high for him.

## Consequences

- `body` is on every turn's menu, about 2,000 characters of definition.
- One call reads the log at most three times, from the first of the 14 days before to the
  window's end. On the Pi, the five days of measurements so far read in about 25 ms.
- A measure Health adds later is not read until it is added to the tool's list.
- The usual level needs 3 days with records, so a measure he starts wearing for has none at first.
- A gap shows charge readings only once the watch app that reports its charge is installed
  (ADR 0377). Until then a gap is named without a cause.
