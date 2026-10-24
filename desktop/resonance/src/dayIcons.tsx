import { Bag, Barbell, Bell, BookOpen, Bicycle, Briefcase, Bus, CalendarBlank, Car, ChatCircleDots, Cloud, CloudFog, CloudLightning, CloudRain, CloudSnow, CloudSun, ChatText, Coins, Couch, DeviceMobile, FlowerLotus, ForkKnife, GraduationCap, Heartbeat, House, Image as PictureIcon, Laptop, MapPin, Moon, MusicNote,
  PencilSimpleLine, PersonSimpleHike, PersonSimpleRun, PersonSimpleSwim, PersonSimpleWalk, Phone, Question, Sparkle, Sun, TerminalWindow, Users, Wind, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import type { Icon, Sky } from './dayView';
import claudeLogo from './assets/brands/claude.svg';

// The glyph of each kind of thing in the day, in the rows and on the bar.
export const ICONS: Record<Exclude<Icon, 'claude'>, PhosphorIcon> = { moon: Moon, walk: PersonSimpleWalk, run: PersonSimpleRun, bike: Bicycle, hike: PersonSimpleHike, swim: PersonSimpleSwim, strength: Barbell, yoga: FlowerLotus, sport: Heartbeat,
  car: Car, bus: Bus, talk: ChatCircleDots, picture: PictureIcon, event: CalendarBlank, reminder: Bell, work: Laptop, briefcase: Briefcase, code: TerminalWindow, call: Phone, home: House, school: GraduationCap, book: BookOpen, pin: MapPin, payment: Coins, chat: Sparkle,
  meal: ForkKnife, sofa: Couch, bag: Bag, people: Users, unknown: Question, music: MusicNote, message: ChatText, device: DeviceMobile, note: PencilSimpleLine };
export const SKIES: Record<Sky, PhosphorIcon> = { sun: Sun, cloudSun: CloudSun, cloud: Cloud, rain: CloudRain, storm: CloudLightning, snow: CloudSnow, fog: CloudFog, wind: Wind };
// A Claude Code session is the Claude mark (the Agents door's); a small cloud on its corner says it is a cloud thread running on this Mac.
export function Ic({ icon, size = 13, cloud = false }: { icon: Icon; size?: number; cloud?: boolean }) {
  const glyph = icon === 'claude' ? <span className="dt-logo" style={{ width: size, height: size, maskImage: `url(${JSON.stringify(claudeLogo)})` }} aria-hidden="true"/> : (() => { const Glyph = ICONS[icon]; return <Glyph size={size} aria-hidden="true"/>; })();
  return cloud ? <span className="dt-cloud">{glyph}<Cloud className="dt-cloud-b" size={Math.max(7, Math.round(size * .62))} weight="fill" aria-hidden="true"/></span> : glyph;
}
