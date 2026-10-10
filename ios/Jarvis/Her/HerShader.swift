import Foundation

// Her's glass shader, compiled at runtime by HerMetal. It lives in a Swift string because this machine has no
// Metal Toolchain (`xcodebuild -downloadComponent MetalToolchain`); with one, paste this text into Her.metal and
// let HerMetal use makeDefaultLibrary() instead.
enum HerShaderSource {
    static let msl = #"""
#include <metal_stdlib>
using namespace metal;

// Her's glass: a line-by-line port of the prototype's GLSL ES 3.0 fragment shader (glass skin keeps the same uniforms).
// One full-screen triangle; `v` is the position on the glass in ball radii (-B...B, y down), as in the vertex stage there.

struct HerU {
    float3x3 rot;
    float3 n1, n2, n3, rim, eyeC, eyeL, eyeR;
    float px, t, q, neb, stars, soft, gal, aur, refr, bright, b, pad;
};

struct HerVOut {
    float4 pos [[position]];
    float2 v;
};

vertex HerVOut herVertex(uint vid [[vertex_id]], constant HerU &u [[buffer(0)]]) {
    float2 p = float2(float((vid << 1) & 2), float(vid & 2)) * 2.0 - 1.0;
    HerVOut o;
    o.pos = float4(p, 0.0, 1.0);
    o.v = float2(p.x, -p.y) * u.b;
    return o;
}

// GLSL's smoothstep also works with reversed edges; Metal leaves that undefined, so spell it out.
static float sst(float a, float b, float x) {
    float t = clamp((x - a) / (b - a), 0.0, 1.0);
    return t * t * (3.0 - 2.0 * t);
}

static float h31(float3 p) {
    p = fract(p * float3(.1031, .1030, .0973));
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
}

static float3 h33(float3 p) {
    p = fract(p * float3(.1031, .1030, .0973));
    p += dot(p, p.yxz + 33.33);
    return fract((p.xxy + p.yxx) * p.zyx);
}

static float vn(float3 x) {
    float3 i = floor(x), f = fract(x);
    f = f * f * (3. - 2. * f);
    return mix(mix(mix(h31(i), h31(i + float3(1., 0., 0.)), f.x), mix(h31(i + float3(0., 1., 0.)), h31(i + float3(1., 1., 0.)), f.x), f.y),
               mix(mix(h31(i + float3(0., 0., 1.)), h31(i + float3(1., 0., 1.)), f.x), mix(h31(i + float3(0., 1., 1.)), h31(i + float3(1., 1., 1.)), f.x), f.y), f.z);
}

static float fbm(float3 p, float q) {
    float s = 0., a = .5, n = 0.;
    for (int i = 0; i < 5; i++) {
        if (float(i) >= q) break;
        s += a * vn(p); n += a;
        p = p * 2.03 + float3(3.1, 1.7, 5.3);
        a *= .5;
    }
    return s / n;
}

static float3 shell(float3 e, float3 d, float r, float farSide, float dens, constant HerU &u) {
    float b = dot(e, d), c = dot(e, e) - r * r, disc = b * b - c;
    if (disc < 0.) return float3(0.);
    float t = farSide > .5 ? -b + sqrt(disc) : -b - sqrt(disc);
    if (t < 0.) return float3(0.);
    float3 q = u.rot * (e + d * t);
    float3 g = q * dens, id = floor(g), f = fract(g);
    float3 hh = h33(id);
    if (hh.x > .4) return float3(0.);
    float3 j = .25 + .5 * h33(id + 17.);
    float dd = length(f - j);
    float pix = u.px * dens * 1.4;
    float sz0 = mix(.035, .085, hh.y * hh.y) * (1. + u.soft * .8);
    float sz = min(max(sz0, pix), .14), k = sz0 / max(sz0, pix);
    k *= k;
    float core = sst(sz, 0., dd) * k * (1. - u.soft * .6);
    float halo = exp(-dd * dd / (sz * sz * (2.5 + u.soft * 2.))) * (1. - sst(.16, .245, dd)) * .2 * k * (1. + u.soft * 2.5);
    float tw = .6 + .4 * sin(u.t * (1.3 + 3.1 * hh.z) + hh.y * 40.);
    float3 col = mix(float3(.74, .82, 1.), float3(1., .87, .74), step(.8, hh.z));
    return col * (core + halo) * tw * (.9 + 4. * hh.y * hh.y);
}

fragment float4 herFragment(HerVOut in [[stage_in]], constant HerU &u [[buffer(0)]]) {
    float2 v = in.v;
    // GL's gl_FragCoord has its origin at the bottom left; the noise seeds follow that.
    float2 fragCoord = float2(in.pos.x, 2.0 * u.b / u.px - in.pos.y);
    float r = length(v), aa = u.px * 1.2;
    if (r > 1.) {
        float halo = exp(-(r - 1.) * 22.) * .07 * min(u.bright, 1.2);
        float3 hc = u.rim * halo;
        return float4(hc, max(hc.r, max(hc.g, hc.b)));
    }
    float z = sqrt(max(0., 1. - r * r));
    float3 n = float3(v, z), I = float3(0., 0., -1.);
    float3 d = normalize(mix(I, refract(I, n, 1. / 1.5), u.refr));
    float3 e = n;
    float tEx = -2. * dot(e, d);
    float3 col = float3(.006, .008, .02);
    float trans = 1.;
    int N = int(u.q * 2.5);
    float dt = tEx / float(N), jit = h31(float3(fragCoord, 3.));
    float3 eL = float3(u.eyeL.xy, sqrt(max(0., 1. - dot(u.eyeL.xy, u.eyeL.xy))) * .88);
    float3 eR = float3(u.eyeR.xy, sqrt(max(0., 1. - dot(u.eyeR.xy, u.eyeR.xy))) * .88);
    for (int i = 0; i < 12; i++) {
        if (i >= N) break;
        float3 p = e + d * ((float(i) + jit) * dt);
        float3 q = u.rot * p;
        float fall = 1. - dot(p, p) * .5, dn = 0.;
        float3 em = float3(0.);
        if (u.neb > 0.) {
            float den = sst(.44, .76, fbm(q * 1.7 + float3(0., u.t * .03, 0.), u.q)) * u.neb * fall;
            float3 c = mix(u.n1, u.n2, sst(.3, .75, vn(q * 2.1 + 7.)));
            c = mix(c, u.n3, sst(.55, .9, vn(q * 1.3 + 13.)));
            em += c * den * (2.4 + 3. * den * den); dn += den;
        }
        if (u.aur > 0.) {
            float hg = q.y + .22 * sin(q.x * 3.1 + u.t * .5) + .12 * sin(q.z * 4.7 - u.t * .37);
            float den = exp(-hg * hg * 50.) * (.3 + .7 * vn(float3(q.x * 9., q.z * 9., u.t * .25))) * u.aur * fall;
            float3 c = mix(float3(.15, 1., .62), float3(.6, .35, 1.), sst(-.4, .5, q.x + q.y * .6));
            em += mix(c, u.n2 * 1.6, .3) * den * 3.2; dn += den * .4;
        }
        if (u.gal > 0.) em += float3(1., .8, .58) * exp(-dot(q, q) * 26.) * 2.2 * u.gal;
        float l = exp(-dot(p - eL, p - eL) * 11.) * u.eyeL.z + exp(-dot(p - eR, p - eR) * 11.) * u.eyeR.z;
        em += u.eyeC * l * (.05 + dn * 1.4);
        col += trans * em * dt;
        trans *= exp(-dn * dt * 2.);
    }
    if (u.gal > 0.) {
        float ct = cos(1.15), st = sin(1.15);
        float3x3 G = float3x3(float3(1., 0., 0.), float3(0., ct, st), float3(0., -st, ct));
        float3 q0 = G * (u.rot * e), qd = G * (u.rot * d);
        float tp = -q0.y / qd.y;
        if (tp > 0. && tp < tEx) {
            float3 gp = q0 + qd * tp;
            float rr = length(gp.xz), ang = atan2(gp.z, gp.x);
            float arm = pow(.5 + .5 * cos(2. * ang - 7.5 * log(rr + .04)), 3.);
            float den = exp(-rr * 4.) * (.2 + 1.4 * arm) * sst(.95, .3, rr);
            den *= .5 + sst(.35, .7, fbm(gp * 7., u.q));
            float3 c = mix(float3(1., .86, .62), mix(float3(.55, .66, 1.), u.n2 * 1.8, .35), sst(.04, .32, rr));
            c += float3(1., .45, .75) * sst(.72, .9, vn(gp * 22.)) * arm * sst(.9, .3, rr) * 1.6;
            col += trans * c * den * u.gal * .45 / max(.3, abs(qd.y));
        }
    }
    float3 farC = shell(e, d, .9, 1., 7., u) + shell(e, d, .72, 1., 9., u) * .8;
    float3 nearC = shell(e, d, .9, 0., 7., u) + shell(e, d, .72, 0., 9., u) * .9 + shell(e, d, .54, 0., 12., u) * .75 + shell(e, d, .36, 0., 16., u) * .6;
    col += (farC * trans * .6 + nearC * mix(1., trans, .4)) * u.stars;
    col *= u.bright;
    col *= mix(.45, 1., sst(0., .6, z));
    col += u.rim * pow(1. - z, 6.) * .5 * min(u.bright, 1.2);
    col = 1. - exp(-col * 1.1);
    col += (h31(float3(fragCoord, 9.)) - .5) / 255.;
    float cov = 1. - sst(1. - aa, 1., r);
    return float4(clamp(col, 0., 1.) * cov, cov);
}
"""#
}
