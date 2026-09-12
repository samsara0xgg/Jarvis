export interface GlassOcclusion { x: number; y: number; width: number; height: number; radius: number }

// Stacked cards represent opaque occlusion with translucent surfaces. The rear
// surface must not tint (or blur) the desktop a second time behind the front one.
export function clipStackGlass(card: HTMLElement): GlassOcclusion | undefined {
  const front = card.previousElementSibling;
  if (!card.matches('.result')) return;
  if (!(front instanceof HTMLElement) || !front.matches('.result')) {
    card.style.removeProperty('clip-path');
    return;
  }
  const back = card.getBoundingClientRect(), cover = front.getBoundingClientRect();
  if (!back.width || !back.height || !cover.height || cover.bottom <= back.top || cover.top >= back.bottom) {
    card.style.removeProperty('clip-path');
    return;
  }
  const occlusion = { x: cover.x - back.x, y: cover.y - back.y, width: cover.width, height: cover.height, radius: Math.min(Number(front.dataset.glass), cover.height / 2) };
  // CSS paths use untransformed local coordinates; native masks use window pixels.
  const sx = back.width / card.offsetWidth, sy = back.height / card.offsetHeight;
  const x = occlusion.x / sx, y = occlusion.y / sy, w = occlusion.width / sx, h = occlusion.height / sy;
  const rx = occlusion.radius / sx, ry = occlusion.radius / sy;
  const hole = `M ${x + rx} ${y} H ${x + w - rx} A ${rx} ${ry} 0 0 1 ${x + w} ${y + ry} V ${y + h - ry} A ${rx} ${ry} 0 0 1 ${x + w - rx} ${y + h} H ${x + rx} A ${rx} ${ry} 0 0 1 ${x} ${y + h - ry} V ${y + ry} A ${rx} ${ry} 0 0 1 ${x + rx} ${y} Z`;
  // Include the shadow outside the rear card, then subtract the front silhouette.
  card.style.clipPath = `path(evenodd, 'M -24 -24 H ${card.offsetWidth + 24} V ${card.offsetHeight + 24} H -24 Z ${hole}')`;
  return occlusion;
}
