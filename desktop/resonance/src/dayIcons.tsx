import { Bell, Bicycle, Bus, CalendarBlank, Car, ChatCircleDots, Coins, GraduationCap, House, Image as PictureIcon, Laptop, MapPin, Moon, PersonSimpleRun, PersonSimpleWalk, Phone, Sparkle, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import type { Icon } from './dayView';

// The glyph of each kind of thing in the day, in the rows and on the bar.
export const ICONS: Record<Icon, PhosphorIcon> = { moon: Moon, walk: PersonSimpleWalk, run: PersonSimpleRun, bike: Bicycle, car: Car, bus: Bus, talk: ChatCircleDots, picture: PictureIcon, event: CalendarBlank,
  reminder: Bell, work: Laptop, call: Phone, home: House, school: GraduationCap, pin: MapPin, payment: Coins, chat: Sparkle };
export function Ic({ icon, size = 13 }: { icon: Icon; size?: number }) { const Glyph = ICONS[icon]; return <Glyph size={size} aria-hidden="true"/>; }
