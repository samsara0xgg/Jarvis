import { Armchair, Bag, Barbell, Bell, Bicycle, Bus, CalendarBlank, Car, ChatCircleDots, Cloud, CloudFog, CloudLightning, CloudRain, CloudSnow, CloudSun, ChatText, Coins, DeviceMobile, FlowerLotus, ForkKnife, GraduationCap, Heartbeat, House, Image as PictureIcon, Laptop, MapPin, Moon, MusicNote,
  PencilSimpleLine, PersonSimpleHike, PersonSimpleRun, PersonSimpleSwim, PersonSimpleWalk, Phone, Question, Sparkle, Sun, TerminalWindow, Users, Wind, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import type { Icon, Sky } from './dayView';

// The glyph of each kind of thing in the day, in the rows and on the bar.
export const ICONS: Record<Icon, PhosphorIcon> = { moon: Moon, walk: PersonSimpleWalk, run: PersonSimpleRun, bike: Bicycle, hike: PersonSimpleHike, swim: PersonSimpleSwim, strength: Barbell, yoga: FlowerLotus, sport: Heartbeat,
  car: Car, bus: Bus, talk: ChatCircleDots, picture: PictureIcon, event: CalendarBlank, reminder: Bell, work: Laptop, code: TerminalWindow, call: Phone, home: House, school: GraduationCap, pin: MapPin, payment: Coins, chat: Sparkle,
  meal: ForkKnife, rest: Armchair, bag: Bag, people: Users, unknown: Question, music: MusicNote, message: ChatText, device: DeviceMobile, note: PencilSimpleLine };
export const SKIES: Record<Sky, PhosphorIcon> = { sun: Sun, cloudSun: CloudSun, cloud: Cloud, rain: CloudRain, storm: CloudLightning, snow: CloudSnow, fog: CloudFog, wind: Wind };
export function Ic({ icon, size = 13 }: { icon: Icon; size?: number }) { const Glyph = ICONS[icon]; return <Glyph size={size} aria-hidden="true"/>; }
