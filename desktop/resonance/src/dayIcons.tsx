import { Barbell, Bell, Bicycle, Bus, CalendarBlank, Car, ChatCircleDots, Cloud, CloudFog, CloudLightning, CloudRain, CloudSnow, CloudSun, Coins, FlowerLotus, GraduationCap, Heartbeat, House, Image as PictureIcon, Laptop, MapPin, Moon,
  PersonSimpleHike, PersonSimpleRun, PersonSimpleSwim, PersonSimpleWalk, Phone, Sparkle, Sun, TerminalWindow, Wind, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import type { Icon, Sky } from './dayView';

// The glyph of each kind of thing in the day, in the rows and on the bar.
export const ICONS: Record<Icon, PhosphorIcon> = { moon: Moon, walk: PersonSimpleWalk, run: PersonSimpleRun, bike: Bicycle, hike: PersonSimpleHike, swim: PersonSimpleSwim, strength: Barbell, yoga: FlowerLotus, sport: Heartbeat,
  car: Car, bus: Bus, talk: ChatCircleDots, picture: PictureIcon, event: CalendarBlank, reminder: Bell, work: Laptop, code: TerminalWindow, call: Phone, home: House, school: GraduationCap, pin: MapPin, payment: Coins, chat: Sparkle };
export const SKIES: Record<Sky, PhosphorIcon> = { sun: Sun, cloudSun: CloudSun, cloud: Cloud, rain: CloudRain, storm: CloudLightning, snow: CloudSnow, fog: CloudFog, wind: Wind };
export function Ic({ icon, size = 13 }: { icon: Icon; size?: number }) { const Glyph = ICONS[icon]; return <Glyph size={size} aria-hidden="true"/>; }
