#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
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
static void setNumber(napi_env env, napi_value obj, const char *key, double value) {
  napi_value result; napi_create_double(env, value, &result);
  napi_set_named_property(env, obj, key, result);
}
static NSWindow *windowForHandle(napi_env env, napi_value value) {
  void *bytes = nullptr; size_t length = 0;
  if (napi_get_buffer_info(env, value, &bytes, &length) != napi_ok || length != sizeof(void *)) return nil;
  return ((__bridge NSView *)*reinterpret_cast<void **>(bytes)).window;
}
static napi_value setStationary(napi_env env, napi_callback_info info) {
  size_t argc = 2; napi_value args[2]; napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  bool enabled = false;
  NSWindow *window = argc == 2 ? windowForHandle(env, args[0]) : nil;
  if (!window || napi_get_value_bool(env, args[1], &enabled) != napi_ok) {
    napi_value result; napi_get_null(env, &result); return result;
  }
  // These two groups are mutually exclusive. Preserve Electron's other flags
  // (Spaces/fullscreen) and restore the original groups when leaving the notch.
  const NSWindowCollectionBehavior mask = NSWindowCollectionBehaviorManaged
    | NSWindowCollectionBehaviorTransient | NSWindowCollectionBehaviorStationary
    | NSWindowCollectionBehaviorParticipatesInCycle | NSWindowCollectionBehaviorIgnoresCycle;
  static char originalBehaviorKey;
  NSNumber *original = objc_getAssociatedObject(window, &originalBehaviorKey);
  NSWindowCollectionBehavior behavior = window.collectionBehavior;
  if (enabled) {
    if (!original) objc_setAssociatedObject(window, &originalBehaviorKey, @(behavior & mask), OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    window.collectionBehavior = (behavior & ~mask) | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle;
  } else if (original) {
    window.collectionBehavior = (behavior & ~mask) | original.unsignedIntegerValue;
    objc_setAssociatedObject(window, &originalBehaviorKey, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
  }
  napi_value result; napi_create_uint32(env, (uint32_t)window.collectionBehavior, &result); return result;
}
static napi_value windowFrame(napi_env env, NSWindow *window) {
  napi_value result; napi_create_object(env, &result);
  NSRect frame = window.frame;
  // AppKit is bottom-left based; Electron uses the primary display's top-left.
  CGFloat desktopTop = NSMaxY(NSScreen.screens.firstObject.frame);
  setNumber(env, result, "x", frame.origin.x);
  setNumber(env, result, "y", desktopTop - NSMaxY(frame));
  setNumber(env, result, "width", frame.size.width);
  setNumber(env, result, "height", frame.size.height);
  return result;
}
static napi_value getFrame(napi_env env, napi_callback_info info) {
  size_t argc = 1; napi_value args[1]; napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  NSWindow *window = argc == 1 ? windowForHandle(env, args[0]) : nil;
  if (!window) { napi_value result; napi_get_null(env, &result); return result; }
  return windowFrame(env, window);
}
static napi_value setFrame(napi_env env, napi_callback_info info) {
  size_t argc = 2; napi_value args[2]; napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  NSWindow *window = argc == 2 ? windowForHandle(env, args[0]) : nil;
  if (!window) { napi_value result; napi_get_null(env, &result); return result; }
  double x = number(env,args[1],"x"), y = number(env,args[1],"y");
  double w = number(env,args[1],"width"), h = number(env,args[1],"height");
  if (w > 0 && h > 0) {
    CGFloat desktopTop = NSMaxY(NSScreen.screens.firstObject.frame);
    // Electron's public setBounds constrains y to the menu bar. A borderless
    // panel can use the full screen through public AppKit, as notch apps do.
    [window setFrame:NSMakeRect(x, desktopTop - y - h, w, h) display:YES];
  }
  return windowFrame(env, window);
}
static napi_value screens(napi_env env, napi_callback_info info) {
  napi_value result; napi_create_array(env, &result); uint32_t index = 0;
  for (NSScreen *screen in NSScreen.screens) {
    napi_value item; napi_create_object(env, &item);
    double top = 0, width = 0;
    if (@available(macOS 12.0, *)) {
      top = screen.safeAreaInsets.top;
      if (top > 0) width = NSMinX(screen.auxiliaryTopRightArea) - NSMaxX(screen.auxiliaryTopLeftArea);
    }
    setNumber(env, item, "id", [screen.deviceDescription[@"NSScreenNumber"] doubleValue]);
    setNumber(env, item, "topInset", top);
    setNumber(env, item, "notchWidth", MAX(0, width));
    napi_set_element(env, result, index++, item);
  }
  return result;
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
  napi_set_named_property(env, exports, "update", fn);
  napi_create_function(env, "getFrame", NAPI_AUTO_LENGTH, getFrame, nullptr, &fn); napi_set_named_property(env, exports, "getFrame", fn);
  napi_create_function(env, "setFrame", NAPI_AUTO_LENGTH, setFrame, nullptr, &fn); napi_set_named_property(env, exports, "setFrame", fn);
  napi_create_function(env, "screens", NAPI_AUTO_LENGTH, screens, nullptr, &fn); napi_set_named_property(env, exports, "screens", fn);
  napi_create_function(env, "setStationary", NAPI_AUTO_LENGTH, setStationary, nullptr, &fn); napi_set_named_property(env, exports, "setStationary", fn);
  return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, init)
