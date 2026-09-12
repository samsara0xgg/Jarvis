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
  napi_get_named_property(env, obj, key, &value); napi_get_value_double(env, value, &result);
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
  }
  [CATransaction commit];
  napi_value result; napi_get_boolean(env, true, &result); return result;
}
static napi_value init(napi_env env, napi_value exports) {
  napi_value fn; napi_create_function(env, "update", NAPI_AUTO_LENGTH, update, nullptr, &fn);
  napi_set_named_property(env, exports, "update", fn); return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, init)
