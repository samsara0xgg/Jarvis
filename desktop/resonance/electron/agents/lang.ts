// ADR 0109: Startrail speaks Jarvis's own language (`language` in settings.yaml, which the daemon serves on
// /inherent/language). English when it says 'en', otherwise the Chinese everything was written in.
// `tr('中文', 'English')` picks one at the moment it runs, so a string built once at load must be built in a function.
export let en = false;
export const setLang = (code: unknown) => { en = code === 'en'; };
export const tr = <T>(zh: T, e: T): T => en ? e : zh;
// English plural: "1 file", "3 files".
export const plural = (n: number, one: string) => `${n} ${n === 1 ? one : `${one}s`}`;
