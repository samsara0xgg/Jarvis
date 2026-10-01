// Where her spoken line stands, from the audio rather than a timer (ADR 0112): the daemon reports the text it has played,
// and this finds how many characters of the displayed line that covers. The daemon speaks the line without its markdown
// and emoji and joins its `<voice>` parts without a break, so those, and spacing, are skipped on either side.
const SKIP = /[\s*`_#]|\p{Extended_Pictographic}/u;
// The characters of `text` that `heard` covers; undefined when `heard` is not the beginning of `text`.
export function heardCount(text: string, heard: string): number | undefined {
  const t = [...text], h = [...heard];
  let i = 0, j = 0;
  while (j < h.length) {
    if (i < t.length && t[i] === h[j]) { i++; j++; }
    else if (i < t.length && SKIP.test(t[i])) i++;
    else if (/\s/.test(h[j])) j++;
    else return undefined;
  }
  return i;
}
