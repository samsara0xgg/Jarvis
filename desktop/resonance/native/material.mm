#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>
#include <node_api.h>
#include <cmath>
// Public AppKit only. Neutral behind-window material, clipped to the actual controls.
// This view never handles input; all controls remain Electron/React.
@interface JarvisMaterialView : NSVisualEffectView
@end
@implementation JarvisMaterialView
- (NSView *)hitTest:(NSPoint)point { return nil; }
@end
static double number(napi_env env, napi_value obj, const char *key) {
  napi_value value; double result = 0;
  if (napi_get_named_property(env, obj, key, &value) != napi_ok ||
      napi_get_value_double(env, value, &result) != napi_ok) return 0;
  return std::isfinite(result) ? result : 0;
}
static napi_value update(napi_env env, napi_callback_info info) {
  size_t argc = 3; napi_value args[3]; napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  if (argc < 3) return nullptr;
  void *bytes = nullptr; size_t length = 0;
  if (napi_get_buffer_info(env, args[0], &bytes, &length) != napi_ok || length != sizeof(void *)) return nullptr;
  NSView *content = (__bridge NSView *)*reinterpret_cast<void **>(bytes);
  if (!content.window) return nullptr;
  double strength = 1; napi_get_value_double(env, args[2], &strength);
  strength = std::isfinite(strength) ? MIN(1, MAX(0, strength)) : 1;
  uint32_t count = 0; napi_get_array_length(env, args[1], &count); count = MIN(count, 16);
  // Keep native material immediately behind Chromium's content, not over the whole window.
  NSView *host = content.superview;
  if (!host) return nullptr;
  NSMutableArray<JarvisMaterialView *> *views = [NSMutableArray array];
  for (NSView *view in host.subviews) if ([view isKindOfClass:[JarvisMaterialView class]]) [views addObject:(JarvisMaterialView *)view];
  while (views.count > count) { [views.lastObject removeFromSuperview]; [views removeLastObject]; }
  while (views.count < count) {
    JarvisMaterialView *v = [[JarvisMaterialView alloc] initWithFrame:NSZeroRect];
    v.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    v.material = NSVisualEffectMaterialHUDWindow;
    v.state = NSVisualEffectStateActive;
    v.appearance = [NSAppearance appearanceNamed:NSAppearanceNameVibrantDark];
    v.wantsLayer = YES; v.layer.masksToBounds = YES;
    [host addSubview:v positioned:NSWindowBelow relativeTo:content]; [views addObject:v];
  }
  [CATransaction begin]; [CATransaction setDisableActions:YES];
  for (uint32_t i = 0; i < count; i++) {
    napi_value r; napi_get_element(env, args[1], i, &r);
    double x = number(env,r,"x"), y = number(env,r,"y"), w = number(env,r,"width"), h = number(env,r,"height");
    NSRect rect = NSMakeRect(x, content.isFlipped ? y : content.bounds.size.height - y - h, w, h);
    views[i].frame = [content convertRect:rect toView:host];
    views[i].layer.cornerRadius = MAX(0, number(env,r,"radius"));
    views[i].alphaValue = strength * MIN(1, MAX(0, number(env,r,"opacity")));
    // Match the renderer's front-card cutout. CSS clipping alone cannot prevent
    // two behind-window visual-effect views from compositing in the same pixels.
    napi_value cover; bool hasCover = false;
    napi_has_named_property(env, r, "occlusion", &hasCover);
    if (hasCover) {
      napi_valuetype type;
      napi_get_named_property(env, r, "occlusion", &cover);
      napi_typeof(env, cover, &type);
      hasCover = type == napi_object;
    }
    double cw = hasCover ? number(env,cover,"width") : 0;
    double ch = hasCover ? number(env,cover,"height") : 0;
    if (cw > 0 && ch > 0) {
      double cx = number(env,cover,"x"), cy = number(env,cover,"y");
      double cr = MAX(0, MIN(number(env,cover,"radius"), MIN(cw, ch) / 2));
      double radius = MAX(0, MIN(number(env,r,"radius"), MIN(w, h) / 2));
      // Use the visual-effect view's own alpha mask so the backdrop itself is
      // clipped, including the WindowServer-composited behind-window material.
      views[i].maskImage = [NSImage imageWithSize:NSMakeSize(w, h) flipped:NO drawingHandler:^BOOL(NSRect bounds) {
        CGContextRef context = NSGraphicsContext.currentContext.CGContext;
        CGMutablePathRef surface = CGPathCreateMutable();
        CGPathAddRoundedRect(surface, nullptr, CGRectMake(0, 0, w, h), radius, radius);
        CGContextAddPath(context, surface);
        CGContextSetRGBFillColor(context, 1, 1, 1, 1);
        CGContextFillPath(context);
        CGPathRelease(surface);
        CGMutablePathRef cutout = CGPathCreateMutable();
        CGPathAddRoundedRect(cutout, nullptr, CGRectMake(cx, h - cy - ch, cw, ch), cr, cr);
        CGContextAddPath(context, cutout);
        CGContextSetBlendMode(context, kCGBlendModeClear);
        CGContextFillPath(context);
        CGPathRelease(cutout);
        return YES;
      }];
    } else views[i].maskImage = nil;
  }
  [CATransaction commit];
  napi_value result; napi_get_boolean(env, true, &result); return result;
}
static napi_value init(napi_env env, napi_value exports) {
  napi_value fn; napi_create_function(env, "update", NAPI_AUTO_LENGTH, update, nullptr, &fn);
  napi_set_named_property(env, exports, "update", fn); return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, init)
