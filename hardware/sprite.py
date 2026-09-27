"""Desktop sprite enclosure, modelled in Blender.

    blender -b --factory-startup --python-exit-code 1 -P hardware/sprite.py
    blender ... -P hardware/sprite.py -- --no-render

Builds the printed parts around stand-ins for the bought parts, exports each
printed part as an STL, saves sprite.blend, renders previews, and exits 1 if a
stand-in or the glass dome intersects anything. Sizes come from params.py
(millimetres); outputs go to hardware/out/.

World axes: X right, Y away from the viewer (the front faces -Y), Z up, base
bottom-front edge on the X axis. The base is a slab leaning back by FACE_TILT;
parts inside it are placed in the face frame (x across, y into the slab, z up
the face). The ball is built upright (screen facing +Z, shell centre at the
origin) and exported that way, then tilted onto the collar.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
BACKDROP = (0.93, 0.93, 0.94)  # preview background, sRGB
sys.path.insert(0, str(HERE))
import params as p  # noqa: E402

BMesh = bmesh.types.BMesh
Obj = bpy.types.Object

# --- Derived layout ----------------------------------------------------------

TILT = math.radians(p.FACE_TILT)
FACE = Matrix.Rotation(-TILT, 4, "X")
FACE_LEN = p.CHIN_H + p.DISPLAY_H + p.TOP_MARGIN
BASE_W = p.DISPLAY_W + 2 * p.SIDE_MARGIN
BASE_H = FACE_LEN * math.cos(TILT)
Y_FRONT_TOP = BASE_H * math.tan(TILT)
THICK = p.BASE_DEPTH * math.cos(TILT)  # slab thickness, square to the face
Y_TOP_MID = Y_FRONT_TOP + p.BASE_DEPTH / 2
DISP_Z = p.CHIN_H + p.DISPLAY_H / 2
DISP_BACK = p.WALL + p.GAP + p.DISPLAY_T

BALL_R = p.BALL_OD / 2
BALL_RI = BALL_R - p.BALL_WALL
RIM_Z = math.sqrt(BALL_R**2 - (p.DOME_OD / 2) ** 2)  # shell radius equals the dome's here
SEAT_Z = math.sqrt(BALL_R**2 - (p.COLLAR_ID / 2) ** 2)  # ball centre above the collar lip
BALL_CENTER = Vector((0.0, Y_TOP_MID, BASE_H + p.COLLAR_H + SEAT_Z))


# --- Geometry helpers --------------------------------------------------------


def box(size: tuple[float, float, float], at: tuple[float, float, float] = (0, 0, 0)) -> BMesh:
    """Return an axis-aligned box mesh centred on ``at``."""
    bm = bmesh.new()
    scale = Matrix.Diagonal((*size, 1.0))
    bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.Translation(at) @ scale)
    return bm


def cylinders(
    r: float,
    h: float,
    centers: list[tuple[float, float, float]],
    axis: str = "Z",
    into: BMesh | None = None,
) -> BMesh:
    """Return a mesh (``into`` if given) with a cylinder at each centre, along ``axis``."""
    turn = {"X": Matrix.Rotation(math.pi / 2, 4, "Y"), "Y": Matrix.Rotation(math.pi / 2, 4, "X")}
    bm = into or bmesh.new()
    for c in centers:
        bmesh.ops.create_cone(
            bm,
            cap_ends=True,
            segments=128 if r > 5 else 20,  # noqa: PLR2004
            radius1=r,
            radius2=r,
            depth=h,
            matrix=Matrix.Translation(c) @ turn.get(axis, Matrix()),
        )
    return bm


def sphere(r: float, at: tuple[float, float, float] = (0, 0, 0)) -> BMesh:
    """Return a UV sphere mesh."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(
        bm, u_segments=160, v_segments=80, radius=r, matrix=Matrix.Translation(at)
    )
    return bm


def grid(w: float, h: float, pitch: float) -> list[tuple[float, float]]:
    """Return hole offsets on a square grid filling a ``w`` x ``h`` rectangle."""
    nx, ny = int(w / 2 / pitch), int(h / 2 / pitch)
    return [(i * pitch, j * pitch) for i in range(-nx, nx + 1) for j in range(-ny, ny + 1)]


def add(
    name: str,
    bm: BMesh,
    coll: bpy.types.Collection,
    mat: bpy.types.Material | None = None,
    frame: Matrix | None = None,
) -> Obj:
    """Turn a bmesh into an object linked to ``coll``, placed by ``frame``."""
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    if mat:
        me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    ob.matrix_world = frame or Matrix()
    return ob


def bake(ob: Obj) -> None:
    """Apply every modifier on ``ob`` into its mesh."""
    evaluated = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
    me = bpy.data.meshes.new_from_object(evaluated)
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    bpy.data.meshes.remove(old)


def boolean(ob: Obj, bm: BMesh, op: str, frame: Matrix | None = None) -> None:
    """Apply a boolean between ``ob`` and a throwaway cutter made from ``bm``."""
    cutter = add("cutter", bm, SCRATCH, frame=frame)
    mod = ob.modifiers.new("bool", "BOOLEAN")
    mod.operation = op
    mod.object = cutter
    mod.solver = "EXACT"
    bake(ob)
    bpy.data.objects.remove(cutter)


def rounded(ob: Obj, radius: float) -> None:
    """Round the sharp edges of ``ob``."""
    mod = ob.modifiers.new("round", "BEVEL")
    mod.width = radius
    mod.segments = 10
    mod.limit_method = "ANGLE"
    mod.use_clamp_overlap = True
    bake(ob)


def hollow(ob: Obj, wall: float) -> None:
    """Turn a closed solid into a closed shell ``wall`` thick, growing inward."""
    mod = ob.modifiers.new("shell", "SOLIDIFY")
    mod.thickness = wall
    mod.offset = -1.0
    mod.use_even_offset = True
    bake(ob)


def split(ob: Obj, keep: BMesh, name: str, frame: Matrix | None = None) -> Obj:
    """Cut ``ob`` in two: return the part inside ``keep`` as ``name``; ``ob`` keeps the rest."""
    part = ob.copy()
    part.data = ob.data.copy()
    part.name = name
    ob.users_collection[0].objects.link(part)
    boolean(part, keep.copy(), "INTERSECT", frame)
    boolean(ob, keep, "DIFFERENCE", frame)
    return part


# --- Materials ---------------------------------------------------------------


def material(
    name: str, color: tuple[float, float, float], rough: float = 0.5, **inputs: float
) -> bpy.types.Material:
    """Return a Principled BSDF material; ``inputs`` names sockets with ``_`` for spaces."""
    mat = bpy.data.materials.new(name)
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    for key, value in inputs.items():
        bsdf.inputs[key.replace("_", " ")].default_value = value
    return mat


def nebula() -> bpy.types.Material:
    """Return the emissive stand-in for the AMOLED's star-core animation."""
    mat = bpy.data.materials.new("screen_nebula")
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0, 0, 0, 1)
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 3.5
    noise.inputs["Detail"].default_value = 10.0
    noise.inputs["Distortion"].default_value = 0.8
    ramp_node = nodes.new("ShaderNodeValToRGB")
    ramp = ramp_node.color_ramp
    stops = [
        (0.45, (0, 0, 0)),
        (0.58, (0.22, 0.04, 0.55)),
        (0.7, (0.1, 0.45, 1.0)),
        (0.82, (1.0, 0.8, 0.95)),
    ]
    for el, (pos, col) in zip(ramp.elements, (stops[0], stops[-1]), strict=True):
        el.position, el.color = pos, (*col, 1)
    for pos, col in stops[1:-1]:
        ramp.elements.new(pos).color = (*col, 1)
    links.new(noise.outputs["Fac"], ramp_node.inputs["Fac"])
    links.new(ramp_node.outputs["Color"], bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = 4.0
    return mat


# --- Scene -------------------------------------------------------------------

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.preferences.filepaths.save_version = 0  # no sprite.blend1 backups
SCENE = bpy.context.scene
SCENE.unit_settings.system = "METRIC"
SCENE.unit_settings.scale_length = 0.001
SCENE.unit_settings.length_unit = "MILLIMETERS"


def collection(name: str) -> bpy.types.Collection:
    """Return a new collection linked under the scene."""
    coll = bpy.data.collections.new(name)
    SCENE.collection.children.link(coll)
    return coll


PRINTED, BOUGHT, SCRATCH = collection("printed"), collection("bought"), collection("scratch")

M_BASE = material("base_pla", (0.78, 0.76, 0.72), 0.55)
M_BALL = material("ball_pla", (0.012, 0.012, 0.014), 0.45)
M_GLASS = material("glass", (1, 1, 1), 0.0, Transmission_Weight=1.0, IOR=1.5)
M_PANEL = material("panel_glass", (0.004, 0.004, 0.005), 0.05)
M_SCREEN = nebula()
M_PCB = material("pcb", (0.03, 0.2, 0.08), 0.4)
M_PI = material("pi5", (0.05, 0.35, 0.12), 0.4)
M_MIC = material("mic_array", (0.05, 0.12, 0.45), 0.4)
M_SPEAKER = material("speaker", (0.08, 0.08, 0.09), 0.6)
M_RADAR = material("radar", (0.85, 0.55, 0.08), 0.4)
M_BATTERY = material("battery", (0.55, 0.6, 0.68), 0.3, Metallic=0.6)
M_FLOOR = material("floor", (0.55, 0.55, 0.56), 0.8)


def face(x: float, y: float, z: float) -> Matrix:
    """Return the world matrix of a point given in the face frame."""
    return FACE @ Matrix.Translation((x, y, z))


# --- Base --------------------------------------------------------------------


def build_base() -> list[Obj]:
    """Build the base shell, back cover and their stand-ins; return the stand-ins."""
    bm = bmesh.new()
    d = p.BASE_DEPTH
    profile = [(0.0, 0.0), (d, 0.0), (d + Y_FRONT_TOP, BASE_H), (Y_FRONT_TOP, BASE_H)]
    verts = [bm.verts.new((-BASE_W / 2, y, z)) for y, z in profile]
    ext = bmesh.ops.extrude_face_region(bm, geom=[bm.faces.new(verts)])
    moved = [v for v in ext["geom"] if isinstance(v, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(BASE_W, 0, 0), verts=moved)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    shell = add("base_shell", bm, PRINTED, M_BASE)
    rounded(shell, p.CORNER_R)
    hollow(shell, p.WALL)
    wall, gap = p.WALL, p.GAP

    window = (p.DISPLAY_ACTIVE_W + 2, wall * 4, p.DISPLAY_ACTIVE_H + 2)
    boolean(shell, box(window, (0, 0, DISP_Z + p.DISPLAY_ACTIVE_DZ)), "DIFFERENCE", FACE)

    spk_x = BASE_W / 2 - wall - gap - p.SPEAKER_D / 2
    spk_y = (DISP_BACK + THICK - wall) / 2
    spk_z = FACE_LEN / 2
    holes = [
        (sx, spk_y + u, spk_z + v)
        for sx in (-BASE_W / 2, BASE_W / 2)
        for v, u in grid(p.GRILLE_LEN, p.GRILLE_WIDTH, p.GRILLE_PITCH)
    ]
    boolean(shell, cylinders(p.GRILLE_HOLE / 2, wall * 6, holes, "X"), "DIFFERENCE", FACE)

    collar = (0.0, Y_TOP_MID, BASE_H + (p.COLLAR_H - wall / 2) / 2)
    boolean(shell, cylinders(p.COLLAR_OD / 2, p.COLLAR_H + wall / 2, [collar]), "UNION")
    well = (0.0, Y_TOP_MID, BASE_H + (p.COLLAR_H + 1) / 2 + 0.01)
    boolean(shell, cylinders(p.COLLAR_ID / 2, p.COLLAR_H + 1, [well]), "DIFFERENCE")
    boolean(shell, sphere(BALL_R + p.SEAT_CLEARANCE, tuple(BALL_CENTER)), "DIFFERENCE")
    mics = [(dx, Y_TOP_MID + dy, BASE_H) for dx, dy in p.MIC_XY]
    boolean(shell, cylinders(p.MIC_HOLE / 2, wall * 4, mics), "DIFFERENCE")

    cut = THICK - wall - 1.0
    cover = box((BASE_W + 20, 60, FACE_LEN + 120), (0, cut + 30, FACE_LEN / 2))
    back = split(shell, cover, "base_back", FACE)
    back_z0 = THICK * math.tan(TILT)  # face-frame z where the back face meets the floor
    vents = [(p.PI_DX + i * p.VENT_PITCH, THICK, DISP_Z) for i in range(-5, 6)]
    for v in vents:
        boolean(back, box((p.VENT_W, 12, p.VENT_H), v), "DIFFERENCE", FACE)
    cable = (0, THICK, back_z0 + wall + 5 + p.CABLE_H / 2)
    boolean(back, box((p.CABLE_W, 12, p.CABLE_H), cable), "DIFFERENCE", FACE)

    pi_y = DISP_BACK + p.PI_STANDOFF + p.PI_T / 2
    mic_z = BASE_H - wall - gap - p.MIC_T / 2
    radar_y = wall + p.RADAR_GAP + p.RADAR_T / 2
    return [
        add(
            "touch_display_2",
            box((p.DISPLAY_W, p.DISPLAY_T, p.DISPLAY_H)),
            BOUGHT,
            M_PANEL,
            face(0, DISP_BACK - p.DISPLAY_T / 2, DISP_Z),
        ),
        add(
            "raspberry_pi_5",
            box((p.PI_W, p.PI_T, p.PI_H)),
            BOUGHT,
            M_PI,
            face(p.PI_DX, pi_y, DISP_Z),
        ),
        *(
            add(
                f"speaker_{side}",
                box((p.SPEAKER_D, p.SPEAKER_H, p.SPEAKER_W)),
                BOUGHT,
                M_SPEAKER,
                face(sign * spk_x, spk_y, spk_z),
            )
            for side, sign in (("left", -1), ("right", 1))
        ),
        add(
            "ld2450_radar",
            box((p.RADAR_W, p.RADAR_T, p.RADAR_H)),
            BOUGHT,
            M_RADAR,
            face(0, radar_y, p.CHIN_H / 2),
        ),
        add(
            "respeaker_xvf3800",
            cylinders(p.MIC_D / 2, p.MIC_T, [(0, Y_TOP_MID, mic_z)]),
            BOUGHT,
            M_MIC,
        ),
    ]


# --- Ball --------------------------------------------------------------------


def build_ball() -> tuple[list[Obj], list[Obj]]:
    """Build the ball upright at the origin; return (all ball objects, checked parts)."""
    shell = add("ball_back", sphere(BALL_R), PRINTED, M_BALL)
    boolean(shell, sphere(BALL_RI), "DIFFERENCE")
    boolean(shell, box((100, 100, 50), (0, 0, RIM_Z + 25)), "DIFFERENCE")
    ports = [(x, y, -BALL_R) for x, y in grid(14, 14, p.BALL_GRILLE_PITCH) if x * x + y * y < 49]  # noqa: PLR2004
    boolean(shell, cylinders(p.BALL_GRILLE_HOLE / 2, 12, ports), "DIFFERENCE")
    ring = split(shell, box((100, 100, 50), (0, 0, p.BALL_SEAM_Z + 25)), "ball_front")

    a, h = p.DOME_OD / 2, p.DOME_HEIGHT
    rd = (a * a + h * h) / (2 * h)  # sphere radius of a cap with that base and height
    base_z = RIM_Z + 0.05
    dome_c = (0.0, 0.0, base_z - (rd - h))
    dome = add("glass_dome", sphere(rd, dome_c), BOUGHT, M_GLASS)
    boolean(dome, sphere(rd - p.DOME_WALL, dome_c), "DIFFERENCE")
    boolean(dome, box((100, 100, 50), (0, 0, base_z - 25)), "DIFFERENCE")

    # The glass is the widest part, so it sets how deep the module sinks into the shell.
    r_glass = p.AMOLED_GLASS_D / 2
    top = min(RIM_Z - p.GAP, math.sqrt((BALL_RI - p.GAP) ** 2 - r_glass**2))
    body_h = p.AMOLED_DEPTH - p.AMOLED_GLASS_T
    module = cylinders(r_glass, p.AMOLED_GLASS_T, [(0, 0, top - p.AMOLED_GLASS_T / 2)])
    glass_faces = len(module.faces)
    cylinders(
        p.AMOLED_PCB_D / 2, body_h, [(0, 0, top - p.AMOLED_GLASS_T - body_h / 2)], into=module
    )
    for f in list(module.faces)[glass_faces:]:
        f.material_index = 1
    amoled = add("esp32_s3_amoled_1_75", module, BOUGHT, M_PANEL)
    amoled.data.materials.append(M_PCB)
    screen_disc = cylinders(p.AMOLED_ACTIVE_D / 2, 0.1, [(0, 0, top + 0.08)])
    screen = add("screen", screen_disc, BOUGHT, M_SCREEN)

    bat_top = top - p.AMOLED_DEPTH - p.GAP
    bx, by, bz = p.BATTERY
    battery = add("lipo", box((bx, by, bz), (0, 0, bat_top - bz / 2)), BOUGHT, M_BATTERY)
    sx, sy, sz = p.BALL_SPEAKER
    spk_at = (0, 0, bat_top - bz - p.GAP - sz / 2)
    speaker = add("ball_speaker", box((sx, sy, sz), spk_at), BOUGHT, M_SPEAKER)

    checked = [amoled, battery, speaker, dome]
    return [shell, ring, screen, *checked], checked


# --- Outputs -----------------------------------------------------------------


def export_stls() -> None:
    """Write one binary STL per printed part."""
    for ob in PRINTED.objects:
        for other in bpy.context.view_layer.objects:
            other.select_set(state=False)
        ob.select_set(state=True)
        bpy.ops.wm.stl_export(filepath=str(OUT / f"{ob.name}.stl"), export_selected_objects=True)


def world_tree(ob: Obj) -> BVHTree:
    """Return a BVH of ``ob``'s faces in world space."""
    mw = ob.matrix_world
    verts = [mw @ v.co for v in ob.data.vertices]
    return BVHTree.FromPolygons(verts, [tuple(f.vertices) for f in ob.data.polygons])


def collisions(checked: list[Obj]) -> list[str]:
    """Return each pair where a checked part intersects another checked or printed part."""
    bpy.context.view_layer.update()
    solids = [*checked, *PRINTED.objects]
    trees = {ob.name: world_tree(ob) for ob in solids}
    return [
        f"{a.name} x {b.name}"
        for i, a in enumerate(checked)
        for b in solids[i + 1 :]
        if trees[a.name].overlap(trees[b.name])
    ]


def setup_render() -> Obj:
    """Configure Cycles, lights, world and floor; return the camera."""
    SCENE.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"
    prefs.get_devices()
    for dev in prefs.devices:
        dev.use = True
    SCENE.cycles.device = "GPU"
    SCENE.cycles.samples = 128
    SCENE.cycles.use_denoising = True
    SCENE.render.resolution_x, SCENE.render.resolution_y = 1400, 1050

    world = bpy.data.worlds.new("world")
    background = world.node_tree.nodes["Background"]
    background.inputs["Color"].default_value = (0.8, 0.82, 0.86, 1)
    background.inputs["Strength"].default_value = 0.35
    SCENE.world = world

    for name, rot, energy in (("key", (50, 0, -35), 3.5), ("fill", (60, 0, 150), 1.2)):
        sun = bpy.data.lights.new(name, "SUN")
        sun.energy = energy
        sun.angle = math.radians(8)
        ob = bpy.data.objects.new(name, sun)
        ob.rotation_euler = [math.radians(deg) for deg in rot]
        SCRATCH.objects.link(ob)

    SCENE.render.film_transparent = True
    floor = add("floor", box((60000, 60000, 1), (0, 0, -0.5)), SCRATCH, M_FLOOR)
    floor.is_shadow_catcher = True
    cam = bpy.data.objects.new("camera", bpy.data.cameras.new("camera"))
    cam.data.clip_start, cam.data.clip_end = 1, 100000
    SCRATCH.objects.link(cam)
    SCENE.camera = cam
    return cam


def shoot(cam: Obj, name: str, eye: Vector, target: Vector, lens: float) -> None:
    """Render one still from ``eye`` looking at ``target``."""
    cam.location = eye
    cam.rotation_euler = (target - eye).to_track_quat("-Z", "Y").to_euler()
    cam.data.lens = lens
    path = OUT / f"{name}.png"
    SCENE.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    # Lay the transparent render (floor shadows included) over a flat backdrop.
    img = bpy.data.images.load(str(path))
    px = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(-1, 4)
    alpha = px[:, 3:]
    px[:, :3] = px[:, :3] * alpha + np.array(BACKDROP) * (1 - alpha)
    px[:, 3] = 1.0
    img.pixels.foreach_set(px.ravel())
    img.save()
    bpy.data.images.remove(img)


def render_all(cam: Obj) -> None:
    """Render the solid views, then the same shells see-through to show the layout."""
    mid = Vector((0, Y_TOP_MID - 10, (BALL_CENTER.z + BALL_R) / 2))
    ball = BALL_CENTER
    side = Vector((-780, mid.y, mid.z + 30))
    shoot(cam, "1_front", Vector((-300, -620, 330)), mid, 62)
    shoot(cam, "2_side", side, mid, 60)
    shoot(cam, "3_ball", ball + Vector((-80, -190, 70)), ball, 70)
    for ob in PRINTED.objects:
        if ob.type == "MESH":
            ghost = ob.data.materials[0].copy()
            ghost.node_tree.nodes["Principled BSDF"].inputs["Alpha"].default_value = 0.15
            ob.data.materials[0] = ghost
    shoot(cam, "4_layout_back", Vector((380, 620, 360)), mid, 60)
    shoot(cam, "5_layout_side", side, mid, 60)
    shoot(cam, "6_ball_inside", ball + Vector((-200, -30, 25)), ball, 75)


def main() -> None:
    """Build, export, save, render, then fail on any collision."""
    OUT.mkdir(exist_ok=True)
    stand_ins = build_base()
    ball_objects, ball_checked = build_ball()
    for ob in (*PRINTED.objects, *BOUGHT.objects):
        ob.data.shade_smooth()
        ob.data.set_sharp_from_angle(angle=math.radians(40))
    export_stls()

    pivot = bpy.data.objects.new("ball", None)
    SCENE.collection.objects.link(pivot)
    for ob in ball_objects:
        ob.parent = pivot
    pivot.location = BALL_CENTER
    pivot.rotation_euler = (math.radians(90 - p.BALL_TILT), 0, 0)

    cam = setup_render()
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT / "sprite.blend"))
    if "--no-render" not in sys.argv:
        render_all(cam)

    hits = collisions(stand_ins + ball_checked)
    for hit in hits:
        print("COLLISION", hit)  # noqa: T201
    size = f"{BASE_W:.0f} x {p.BASE_DEPTH + Y_FRONT_TOP:.0f} x {BASE_H:.0f}"
    print(f"sprite: base {size} mm (w x d x h), ball centre {BALL_CENTER.z:.0f} mm up")  # noqa: T201
    if hits:
        raise SystemExit(1)


main()
