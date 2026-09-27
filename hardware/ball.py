"""Fit check for the handheld glass ball, modelled in Blender.

    uv run --no-project --with cadquery-ocp python hardware/board_mesh.py   # once
    blender -b --factory-startup --python-exit-code 1 -P hardware/ball.py

Places the real board (Waveshare's STEP), the glass half-ball and stand-ins for
the battery, speaker and plugs inside a plain printed shell, then reports every
collision and the tightest gaps, sweeps the largest speaker that still fits,
saves ball.blend and renders sections into hardware/out/. Exits 1 on a
collision or a gap under MIN_GAP. Not yet printable: no seam, lock, button,
lanyard or grille features.

Axes: the glass faces +Z, +Y is up when the ball hangs, +X is the viewer's
right looking at the face; the sphere centre is the origin. Sizes in mm.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
BOARD_STL = OUT / "vendor" / "amoled_1_75.stl"

BMesh = bmesh.types.BMesh
Obj = bpy.types.Object

# --- Bought parts: listing sizes; measure yours and re-run ----------------------

GLASS_D, GLASS_H = 60.0, 26.0  # DS. Distinctive Style solid K9 dome, amazon.ca B0BY8LYDF2
BATTERY = (31.0, 30.0, 5.0)  # HXJN 503030 500 mAh, amazon.ca B0CP24RVYK; x, y, thickness
SPEAKER_D, SPEAKER_T = 20.0, 3.6  # MECCANIXITY 20 mm 8 ohm 1 W, amazon.ca B0GR48Q949
PLUG_H = 3.5  # MX1.25 plug standing out of its socket, plus the wires' first bend
USB_PLUG = (12.4, 6.6)  # USB-C cable overmold, width x height; a bulky one

# --- Design choices -------------------------------------------------------------

WALL = 2.0  # printed shell
TAPE = 0.6  # 3M 5925 black VHB between the glass rim and the shell
LIP = 0.8  # printed lip over the display's black border; keeps the glass off the screen
BORE_R = 24.8  # display cover glass is 48.96 across
LIP_R = 22.3  # visible area is 44.16 across
PAD = 2.0  # arms the board's standoffs rest on
MIN_GAP = 0.3  # smallest gap between parts not meant to touch
TOUCH = 0.05  # gap left where parts do touch, so overlap tests stay clean
SOUND_GAP = 1.5  # speaker face to the shell, room for the sound to reach the grille

# --- Board landmarks in the STEP frame (Waveshare drawing + STEP solids) ---------

STEP_FRONT, STEP_GLASS_T = 2.80, 3.46  # cover glass front; glass + panel stack
STANDOFFS = [(13.75, 14.7), (0.0, -20.5), (-13.75, 14.7)]  # (x, z) of the M2 standoffs
STANDOFF_BACK = -7.64
MICS = [(-17.54, 10.96), (-17.54, -10.96)]  # (x, z); top-ported, port face at MIC_FACE
MIC_FACE = -5.23
SOCKETS = {"plug_spk": (2.67, 10.32), "plug_bat": (-10.63, -2.98)}  # x spans
SOCKET_Z, SOCKET_TOP = (-20.12, -15.92), -8.93
USB_MOUTH, USB_Y = -23.80, (-7.39, -3.21)  # receptacle mouth x; y span, centred on z = 0

# --- Derived ----------------------------------------------------------------------

R = (GLASS_D**2 / 4 + GLASS_H**2) / (2 * GLASS_H)  # sphere radius of the glass, and the ball
RI = R - WALL
FACE_Z = R - GLASS_H  # glass flat face, above the centre when the glass is under a half
TOP_Z = FACE_Z - TAPE  # shell top face
SCREEN_Z = TOP_Z - LIP - TOUCH  # display front
# STEP (x, y, z) -> ball (z, x, y + dz): USB-C points down, the pin header to the right.
BOARD = Matrix(((0, 0, 1, 0), (1, 0, 0, 0), (0, 1, 0, SCREEN_Z - STEP_FRONT), (0, 0, 0, 1)))


def at(x: float, y: float, z: float) -> Vector:
    """Map a point from the board's STEP frame into the ball frame."""
    return BOARD @ Vector((x, y, z))


# --- Geometry helpers --------------------------------------------------------------


def box(lo: tuple[float, float, float], hi: tuple[float, float, float]) -> BMesh:
    """Return an axis-aligned box between corners ``lo`` and ``hi``."""
    size = [b - a for a, b in zip(lo, hi, strict=True)]
    mid = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]
    bm = bmesh.new()
    bmesh.ops.create_cube(
        bm, size=1.0, matrix=Matrix.Translation(mid) @ Matrix.Diagonal((*size, 1))
    )
    return bm


def slab(z0: float, z1: float) -> BMesh:
    """Return a box covering the whole ball between heights ``z0`` and ``z1``."""
    return box((-100, -100, z0), (100, 100, z1))


def cylinder(r: float, z0: float, z1: float, xy: tuple[float, float] = (0, 0)) -> BMesh:
    """Return a Z-axis cylinder from ``z0`` to ``z1``."""
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        segments=128 if r > 5 else 32,  # noqa: PLR2004
        radius1=r,
        radius2=r,
        depth=z1 - z0,
        matrix=Matrix.Translation((*xy, (z0 + z1) / 2)),
    )
    return bm


def sphere(r: float) -> BMesh:
    """Return a UV sphere at the origin."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=160, v_segments=80, radius=r)
    return bm


def arm(xy: tuple[float, float], r0: float, width: float, z0: float, z1: float) -> BMesh:
    """Return a bar from radius ``r0`` at ``xy``'s bearing out into the wall."""
    r1 = math.sqrt(RI**2 - max(z0 * z0, z1 * z1)) + WALL / 2
    bm = box((r0, -width / 2, z0), (r1, width / 2, z1))
    bmesh.ops.rotate(bm, verts=bm.verts, matrix=Matrix.Rotation(math.atan2(xy[1], xy[0]), 3, "Z"))
    return bm


def add(name: str, bm: BMesh, coll: bpy.types.Collection, mat: bpy.types.Material) -> Obj:
    """Turn a bmesh into an object in ``coll``."""
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    return ob


def boolean(ob: Obj, cutter: BMesh | Obj, op: str) -> None:
    """Apply a boolean with a throwaway ``cutter`` and bake it into ``ob``'s mesh."""
    tool = cutter if isinstance(cutter, Obj) else add("cutter", cutter, SCRATCH, M_SHELL)
    mod = ob.modifiers.new("bool", "BOOLEAN")
    mod.operation, mod.object, mod.solver = op, tool, "EXACT"
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    old = ob.data
    me.materials.clear()  # the cutter's faces would otherwise bring its colour along
    me.materials.append(old.materials[0])
    me.polygons.foreach_set("material_index", [0] * len(me.polygons))
    ob.modifiers.clear()
    ob.data = me
    bpy.data.meshes.remove(old)
    bpy.data.objects.remove(tool)


def union(
    name: str, pieces: list[BMesh], coll: bpy.types.Collection, mat: bpy.types.Material
) -> Obj:
    """Return one object that is the union of ``pieces``."""
    ob = add(name, pieces[0], coll, mat)
    for bm in pieces[1:]:
        boolean(ob, bm, "UNION")
    return ob


def material(name: str, rgba: tuple[float, float, float, float]) -> bpy.types.Material:
    """Return a flat colour for Workbench renders."""
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = rgba
    return mat


# --- Scene -------------------------------------------------------------------------

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.preferences.filepaths.save_version = 0
SCENE = bpy.context.scene
SCENE.unit_settings.scale_length = 0.001
SCENE.unit_settings.length_unit = "MILLIMETERS"


def collection(name: str) -> bpy.types.Collection:
    """Return a new collection under the scene."""
    coll = bpy.data.collections.new(name)
    SCENE.collection.children.link(coll)
    return coll


PRINTED, BOUGHT, KEEPOUT, SCRATCH = (
    collection(n) for n in ("printed", "bought", "keepout", "scratch")
)
M_SHELL = material("shell", (0.16, 0.16, 0.17, 1))
M_ARMS = material("arms", (0.95, 0.55, 0.15, 1))
M_GLASS = material("glass", (0.62, 0.86, 0.95, 1))
M_BOARD = material("board", (0.1, 0.45, 0.25, 1))
M_BATTERY = material("battery", (0.55, 0.62, 0.78, 1))
M_SPEAKER = material("speaker", (0.75, 0.2, 0.25, 1))
M_KEEPOUT = material("keepout", (0.95, 0.85, 0.2, 1))


# --- Build -------------------------------------------------------------------------


def build() -> float:
    """Build every part; return the battery's bottom face height."""
    glass = add("glass", sphere(R), BOUGHT, M_GLASS)
    boolean(glass, slab(FACE_Z, R + 1), "INTERSECT")

    bpy.ops.wm.stl_import(filepath=str(BOARD_STL))
    board = bpy.context.selected_objects[0]
    board.name = "board"
    board.data.materials.append(M_BOARD)
    for coll in board.users_collection:
        coll.objects.unlink(board)
    BOUGHT.objects.link(board)
    board.matrix_world = BOARD

    shell = add("shell", sphere(R), PRINTED, M_SHELL)
    boolean(shell, slab(TOP_Z, R + 1), "DIFFERENCE")
    bore_bottom = SCREEN_Z - STEP_GLASS_T - 0.5
    cavity = add("cavity", sphere(RI), SCRATCH, M_SHELL)
    boolean(cavity, slab(bore_bottom, R + 1), "DIFFERENCE")
    boolean(shell, cavity, "DIFFERENCE")
    boolean(shell, cylinder(BORE_R, bore_bottom - 0.01, SCREEN_Z + TOUCH), "DIFFERENCE")
    boolean(shell, cylinder(LIP_R, SCREEN_Z, R + 1), "DIFFERENCE")

    usb_z = at(0, sum(USB_Y) / 2, 0).z
    usb_mouth = at(USB_MOUTH, 0, 0).y - TOUCH
    w, h = USB_PLUG
    add(
        "usb_plug",
        box((-w / 2, -R - 10, usb_z - h / 2), (w / 2, usb_mouth, usb_z + h / 2)),
        KEEPOUT,
        M_KEEPOUT,
    )
    g = MIN_GAP
    usb_cut = box(
        (-w / 2 - g, -R - 10, usb_z - h / 2 - g), (w / 2 + g, usb_mouth + g, usb_z + h / 2 + g)
    )
    boolean(shell, usb_cut, "DIFFERENCE")

    for name, (y0, y1) in SOCKETS.items():
        top = at(0, SOCKET_TOP, 0).z - TOUCH
        add(name, box((SOCKET_Z[0], y0, top - PLUG_H), (SOCKET_Z[1], y1, top)), KEEPOUT, M_KEEPOUT)

    pad_top = at(0, STANDOFF_BACK, 0).z - TOUCH
    pad_bottom = pad_top - PAD
    arms = []
    for x, z in STANDOFFS:
        p = at(x, 0, z)
        arms.append(cylinder(2.0, pad_bottom, pad_top, (p.x, p.y)))
        arms.append(arm((p.x, p.y), math.hypot(p.x, p.y), 3.5, pad_bottom, pad_top))
    union("board_arms", arms, PRINTED, M_ARMS)

    gasket = 1.0  # foam ring between each mic port and its boss
    face = at(0, MIC_FACE, 0).z - gasket
    bosses = []
    for x, z in MICS:
        p = at(x, 0, z)
        bosses.append(arm((p.x, p.y), math.hypot(p.x, p.y) - 2.5, 4.5, face - 2.0, face))
    union("mic_bosses", bosses, PRINTED, M_ARMS)

    bat_top = pad_bottom - MIN_GAP - 0.1
    bx, by, bz = BATTERY
    add(
        "battery",
        box((-bx / 2, -by / 2, bat_top - bz), (bx / 2, by / 2, bat_top)),
        BOUGHT,
        M_BATTERY,
    )
    return bat_top - bz


def add_speaker(bat_bottom: float) -> None:
    """Hang the speaker below the battery in a cup that joins the shell."""
    top = bat_bottom - MIN_GAP - 0.1
    r = SPEAKER_D / 2
    add("speaker", cylinder(r, top - SPEAKER_T, top), BOUGHT, M_SPEAKER)
    cup = add("speaker_cup", cylinder(r + 1.4, -R, top), PRINTED, M_ARMS)
    boolean(cup, cylinder(r + 0.2, -R - 1, top + 1), "DIFFERENCE")
    boolean(cup, sphere(RI + WALL / 2), "INTERSECT")


# --- Checks ------------------------------------------------------------------------

# Pairs built to touch: overlap is still checked, the gap is not.
CONTACT = {
    frozenset(p)
    for p in (
        ("board", "shell"),
        ("board", "board_arms"),
        ("board", "plug_spk"),
        ("board", "plug_bat"),
        ("board", "usb_plug"),
        ("usb_plug", "shell"),
        ("speaker", "speaker_cup"),
    )
}


def world(ob: Obj) -> tuple[list[Vector], BVHTree]:
    """Return ``ob``'s world-space vertices and face BVH."""
    mw = ob.matrix_world
    verts = [mw @ v.co for v in ob.data.vertices]
    return verts, BVHTree.FromPolygons(verts, [tuple(f.vertices) for f in ob.data.polygons])


def gap(a: tuple[list[Vector], BVHTree], b: tuple[list[Vector], BVHTree]) -> float:
    """Return the smallest vertex-to-surface distance between two meshes, both ways."""
    return min([*(b[1].find_nearest(v)[3] for v in a[0]), *(a[1].find_nearest(v)[3] for v in b[0])])


def check() -> tuple[list[str], list[tuple[float, str]]]:
    """Return (collisions, every non-contact gap) between parts that are not both printed."""
    bpy.context.view_layer.update()
    parts = [*BOUGHT.objects, *KEEPOUT.objects, *PRINTED.objects]
    mesh = {ob.name: world(ob) for ob in parts}
    hits, gaps = [], []
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if a.users_collection[0] is PRINTED and b.users_collection[0] is PRINTED:
                continue
            pair = f"{a.name} / {b.name}"
            if mesh[a.name][1].overlap(mesh[b.name][1]):
                hits.append(pair)
            elif frozenset((a.name, b.name)) not in CONTACT:
                gaps.append((gap(mesh[a.name], mesh[b.name]), pair))
    return hits, sorted(gaps)


def speaker_room(bat_bottom: float) -> list[tuple[float, float]]:
    """Return (diameter, largest thickness) for round speakers hung under the battery."""
    shell = world(bpy.data.objects["shell"])
    top = bat_bottom - MIN_GAP - 0.1
    room = []
    for d in (20, 23, 28, 30, 32, 36, 40):
        lo, hi = 0.0, top + RI - SOUND_GAP  # thickness bounds; hi keeps SOUND_GAP at the pole
        while hi - lo > 0.05:  # noqa: PLR2004
            t = (lo + hi) / 2
            probe = add("probe", cylinder(d / 2, top - t, top), SCRATCH, M_SPEAKER)
            fits = not probe_hits(probe, shell)
            bpy.data.objects.remove(probe)
            lo, hi = (t, hi) if fits else (lo, t)
        room.append((d, lo))
    return room


def probe_hits(probe: Obj, shell: tuple[list[Vector], BVHTree]) -> bool:
    """Return True if ``probe`` crosses the shell or comes within MIN_GAP of it."""
    mesh = world(probe)
    return mesh[1].overlap(shell[1]) or gap(mesh, shell) < MIN_GAP


def grams() -> dict[str, float]:
    """Return rough masses: printed parts in PLA, the glass in K9; others from listings."""
    printed = 0.0
    for ob in PRINTED.objects:
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        printed += bm.calc_volume(signed=False)
        bm.free()
    glass = math.pi * GLASS_H**2 * (3 * R - GLASS_H) / 3
    return {
        "glass (K9, 2.51 g/cm3)": glass * 2.51e-3,
        "printed (PLA, 1.24 g/cm3, solid)": printed * 1.24e-3,
        "board (UNVERIFIED)": 15.0,
        "battery 503030 (typical)": 10.0,
        "speaker (typical)": 2.0,
    }


# --- Renders -----------------------------------------------------------------------


def camera() -> Obj:
    """Configure a Workbench scene and return an orthographic camera."""
    SCENE.render.engine = "BLENDER_WORKBENCH"
    shading = SCENE.display.shading
    shading.light, shading.color_type = "STUDIO", "MATERIAL"
    shading.show_cavity = shading.show_object_outline = True
    shading.show_backface_culling = False
    SCENE.render.resolution_x = SCENE.render.resolution_y = 1400
    world_ = bpy.data.worlds.new("world")
    world_.color = (0.96, 0.96, 0.97)
    SCENE.world = world_
    cam = bpy.data.objects.new("camera", bpy.data.cameras.new("camera"))
    cam.data.type, cam.data.ortho_scale = "ORTHO", 2 * R + 14
    SCRATCH.objects.link(cam)
    SCENE.camera = cam
    return cam


def shoot(cam: Obj, name: str, look: Vector, up: Vector, *, cut: bool) -> None:
    """Render along ``look``; with ``cut``, drop the near half at the origin plane."""
    fwd = look.normalized()
    right = fwd.cross(up).normalized()
    rot = Matrix((right, up.normalized(), -fwd)).transposed().to_4x4()
    cam.matrix_world = Matrix.Translation(-fwd * 200) @ rot
    cam.data.clip_start = 199.9 if cut else 1  # the board is clipped, not cut
    cut_copies = []
    if cut:
        span = [(0, 100) if c < -0.5 else (-100, 0) if c > 0.5 else (-100, 100) for c in fwd]  # noqa: PLR2004
        half = box(tuple(s[0] for s in span), tuple(s[1] for s in span))
        for ob in [*PRINTED.objects, *BOUGHT.objects, *KEEPOUT.objects]:
            if ob.name == "board":
                continue
            copy = ob.copy()
            copy.data = ob.data.copy()
            SCRATCH.objects.link(copy)
            boolean(copy, half.copy(), "DIFFERENCE")
            ob.hide_render = True
            cut_copies.append(copy)
        half.free()
    SCENE.render.filepath = str(OUT / f"ball_{name}.png")
    bpy.ops.render.render(write_still=True)
    for copy in cut_copies:
        bpy.data.objects.remove(copy)
    for ob in bpy.data.objects:
        ob.hide_render = False


def render_all() -> None:
    """Render two sections and the layout seen from behind with the shell hidden."""
    cam = camera()
    z = Vector((0, 0, 1))
    shoot(cam, "1_section_side", Vector((-1, 0, 0)), z, cut=True)
    shoot(cam, "2_section_across", Vector((0, -1, 0)), z, cut=True)
    for name in ("shell", "glass"):
        bpy.data.objects[name].hide_render = True
    shoot(cam, "3_from_behind", Vector((0, 0, 1)), Vector((0, 1, 0)), cut=False)
    for ob in bpy.data.objects:
        ob.hide_render = False


# --- Main --------------------------------------------------------------------------


def main() -> None:
    """Build, check, sweep the speaker, save, render; exit 1 on a clash."""
    if not BOARD_STL.exists():
        msg = f"missing {BOARD_STL}: run hardware/board_mesh.py first"
        raise SystemExit(msg)
    OUT.mkdir(exist_ok=True)
    bat_bottom = build()
    room = speaker_room(bat_bottom)
    add_speaker(bat_bottom)
    hits, gaps = check()
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT / "ball.blend"))
    if "--no-render" not in sys.argv:
        render_all()

    lines = [
        f"ball {2 * R:.2f} across, glass face {FACE_Z:.2f} above centre",
        f"display front {SCREEN_Z:.2f}",
        f"battery {BATTERY} bottom at {bat_bottom:.2f}; speaker {SPEAKER_D} x {SPEAKER_T}",
        *(f"COLLISION {pair}" for pair in hits),
        *(f"{'TIGHT' if d < MIN_GAP else 'gap'} {d:5.2f}  {pair}" for d, pair in gaps[:14]),
        "largest round speaker under the battery (diameter: thickness):",
        *(f"  {d:.0f}: {t:.1f}" for d, t in room),
        *(f"mass {k}: {v:.0f} g" for k, v in grams().items()),
    ]
    report = "\n".join(lines)
    print(report)  # noqa: T201
    (OUT / "ball_report.txt").write_text(report + "\n")
    if hits or gaps[0][0] < MIN_GAP:
        raise SystemExit(1)


main()
