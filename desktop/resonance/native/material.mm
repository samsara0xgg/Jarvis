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
// Button state survives a drag changing renderers; AppKit needs no input permission.
static napi_value leftMouseDown(napi_env env, napi_callback_info info) {
  napi_value result; napi_get_boolean(env, (NSEvent.pressedMouseButtons & 1) != 0, &result); return result;
}
// Whether ⌘ is held right now; AppKit answers this without any input permission.
static napi_value commandDown(napi_env env, napi_callback_info info) {
  napi_value result; napi_get_boolean(env, (NSEvent.modifierFlags & NSEventModifierFlagCommand) != 0, &result); return result;
}
// ADR 0058, dictation. The right ⌥ key alone, read like ⌘ above without any input permission, and how long ago
// any key went down: a tap counts only when no other key was typed while it was held.
static napi_value rightOption(napi_env env, napi_callback_info info) {
  const CGEventFlags flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState);
  const CGEventFlags others = kCGEventFlagMaskCommand | kCGEventFlagMaskControl | kCGEventFlagMaskShift | 0x20; // 0x20: left ⌥
  napi_value result; napi_create_object(env, &result);
  napi_value value;
  napi_get_boolean(env, (flags & 0x40) != 0, &value); napi_set_named_property(env, result, "down", value); // 0x40: right ⌥
  napi_get_boolean(env, (flags & others) != 0, &value); napi_set_named_property(env, result, "others", value);
  setNumber(env, result, "keyIdle", CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown));
  return result;
}
// Her shortcut, a double tap of the left ⌘ alone, read the same way: whether it is down, whether any other modifier is.
static napi_value leftCommand(napi_env env, napi_callback_info info) {
  const CGEventFlags flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState);
  const CGEventFlags others = kCGEventFlagMaskAlternate | kCGEventFlagMaskControl | kCGEventFlagMaskShift | 0x10; // 0x10: right ⌘
  napi_value result; napi_create_object(env, &result);
  napi_value value;
  napi_get_boolean(env, (flags & 0x08) != 0, &value); napi_set_named_property(env, result, "down", value); // 0x08: left ⌘
  napi_get_boolean(env, (flags & others) != 0, &value); napi_set_named_property(env, result, "others", value);
  setNumber(env, result, "keyIdle", CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown));
  return result;
}
// Typing to her, a double tap of the left ⌥ alone, read like the left ⌘: the right ⌥ is dictation's (ADR 0058).
static napi_value leftOption(napi_env env, napi_callback_info info) {
  const CGEventFlags flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState);
  const CGEventFlags others = kCGEventFlagMaskCommand | kCGEventFlagMaskControl | kCGEventFlagMaskShift | 0x40; // 0x40: right ⌥
  napi_value result; napi_create_object(env, &result);
  napi_value value;
  napi_get_boolean(env, (flags & 0x20) != 0, &value); napi_set_named_property(env, result, "down", value); // 0x20: left ⌥
  napi_get_boolean(env, (flags & others) != 0, &value); napi_set_named_property(env, result, "others", value);
  setNumber(env, result, "keyIdle", CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown));
  return result;
}
static void setString(napi_env env, napi_value obj, const char *key, NSString *text) {
  napi_value value; napi_create_string_utf8(env, (text ?: @"").UTF8String, NAPI_AUTO_LENGTH, &value);
  napi_set_named_property(env, obj, key, value);
}
static void setRect(napi_env env, napi_value obj, const char *key, CGRect r) {
  napi_value value; napi_create_object(env, &value);
  setNumber(env, value, "x", r.origin.x); setNumber(env, value, "y", r.origin.y);
  setNumber(env, value, "width", r.size.width); setNumber(env, value, "height", r.size.height);
  napi_set_named_property(env, obj, key, value);
}
static CFTypeRef copyAttribute(AXUIElementRef element, CFStringRef name) {
  CFTypeRef value = nullptr;
  return element && AXUIElementCopyAttributeValue(element, name, &value) == kAXErrorSuccess ? value : nullptr;
}
static NSString *textAttribute(AXUIElementRef element, CFStringRef name) {
  CFTypeRef value = copyAttribute(element, name);
  if (value && CFGetTypeID(value) == CFStringGetTypeID()) return (__bridge_transfer NSString *)value;
  if (value) CFRelease(value);
  return nil;
}
// A text range's box in global top-left points; apps that do not know answer nothing or a zero or absurd box.
static bool boundsFor(AXUIElementRef element, CFIndex location, CFIndex length, CGRect *out) {
  CFRange range = CFRangeMake(location, length);
  AXValueRef param = AXValueCreate(kAXValueTypeCFRange, &range);
  CFTypeRef value = nullptr;
  AXError error = AXUIElementCopyParameterizedAttributeValue(element, kAXBoundsForRangeParameterizedAttribute, param, &value);
  CFRelease(param);
  bool ok = error == kAXErrorSuccess && value && AXValueGetValue((AXValueRef)value, kAXValueTypeCGRect, out);
  if (value) CFRelease(value);
  return ok && out->size.height > 1 && out->size.height < 200 && (out->origin.x != 0 || out->origin.y != 0);
}
// ADR 0110, as 言字 sends it: up to 300 characters before the caret, so the polish spells names the way the field
// already does. Asked for as a range so a long document is never read whole; never from a password field.
static NSString *textBefore(AXUIElementRef element, CFIndex caret) {
  if (caret <= 0) return nil;
  for (NSString *role in @[textAttribute(element, kAXRoleAttribute) ?: @"", textAttribute(element, kAXSubroleAttribute) ?: @""])
    if ([role isEqualToString:@"AXSecureTextField"]) return nil;
  const CFIndex start = MAX(0, caret - 300);
  CFRange range = CFRangeMake(start, caret - start);
  AXValueRef param = AXValueCreate(kAXValueTypeCFRange, &range);
  CFTypeRef value = nullptr;
  AXUIElementCopyParameterizedAttributeValue(element, kAXStringForRangeParameterizedAttribute, param, &value);
  CFRelease(param);
  NSString *text = nil;
  if (value && CFGetTypeID(value) == CFStringGetTypeID()) text = (__bridge_transfer NSString *)value;
  else if (value) CFRelease(value);
  if (!text.length) { // an app that answers no ranged query: its whole value, unless it is a document
    NSString *whole = textAttribute(element, kAXValueAttribute);
    if (whole.length >= (NSUInteger)caret && whole.length <= 200000) text = [whole substringWithRange:NSMakeRange(start, caret - start)];
  }
  return [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
}
// ADR 0110, 言字 0.4.0's fix for apps (Claude) whose text box loses focus while the words are polished: the app and
// the text field the dictation started in, held from caret() until the paste.
static pid_t startPid = 0;
static AXUIElementRef startField = nullptr;
static bool isTextField(AXUIElementRef element) {
  if (!element) return false;
  NSString *role = textAttribute(element, kAXRoleAttribute) ?: @"";
  if ([role isEqualToString:@"AXWebArea"]) return false; // a whole page, not a field
  if ([@[@"AXTextArea", @"AXTextField", @"AXComboBox", @"AXSearchField"] containsObject:role]) return true;
  CFTypeRef editable = copyAttribute(element, CFSTR("AXEditable"));
  const bool yes = editable && CFGetTypeID(editable) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)editable);
  if (editable) CFRelease(editable);
  return yes;
}
static bool focusIsTextField(pid_t pid) {
  AXUIElementRef app = AXUIElementCreateApplication(pid);
  AXUIElementSetMessagingTimeout(app, 0.25);
  AXUIElementRef focused = (AXUIElementRef)copyAttribute(app, kAXFocusedUIElementAttribute);
  CFRelease(app);
  const bool yes = isTextField(focused);
  if (focused) CFRelease(focused);
  return yes;
}
// Just before the ⌘V: "ok" the caret is in a text field of the app the dictation started in (put back there if the app
// had moved focus away), "blind" nothing to check against, "elsewhere" another app is in front, "lost" the field is gone
// and could not be focused again. Only "ok" and "blind" are pasted into.
static napi_value pasteTarget(napi_env env, napi_callback_info info) {
  const char *where = "blind";
  const pid_t front = NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;
  if (startPid && front && front != startPid) where = "elsewhere";
  else if (startField && focusIsTextField(startPid)) where = "ok";
  else if (startField) {
    AXUIElementSetAttributeValue(startField, kAXFocusedAttribute, kCFBooleanTrue);
    where = focusIsTextField(startPid) ? "ok" : "lost";
  }
  napi_value result; napi_create_string_utf8(env, where, NAPI_AUTO_LENGTH, &result); return result;
}
// ADR 0058: where the words will land. The focused element's caret (or the end of its selection), the right end of
// the caret's line and the element's frame, in global top-left points like Electron's, plus the app, its window's title
// and the selected text for the polish. Needs Accessibility: without it only `trusted: false` and the app come back.
// An app that shows no caret to Accessibility leaves `caret` out, and she comes up by the mouse.
static napi_value caret(napi_env env, napi_callback_info info) {
  napi_value result; napi_create_object(env, &result);
  NSRunningApplication *front = NSWorkspace.sharedWorkspace.frontmostApplication;
  setString(env, result, "app", front.localizedName);
  const bool trusted = AXIsProcessTrusted();
  napi_value value; napi_get_boolean(env, trusted, &value); napi_set_named_property(env, result, "trusted", value);
  startPid = front.processIdentifier;
  if (startField) { CFRelease(startField); startField = nullptr; }
  if (!trusted || !front) return result;
  AXUIElementRef app = AXUIElementCreateApplication(front.processIdentifier);
  // A hung app must not hold the companion's main thread for AX's default six seconds.
  AXUIElementSetMessagingTimeout(app, 0.25);
  AXUIElementRef window = (AXUIElementRef)copyAttribute(app, kAXFocusedWindowAttribute);
  setString(env, result, "window", textAttribute(window, kAXTitleAttribute));
  if (window) CFRelease(window);
  AXUIElementRef focused = (AXUIElementRef)copyAttribute(app, kAXFocusedUIElementAttribute);
  CFRelease(app);
  if (!focused) return result;
  if (isTextField(focused)) { startField = (AXUIElementRef)CFRetain(focused); AXUIElementSetMessagingTimeout(startField, 0.25); }
  CGPoint origin; CGSize size;
  CFTypeRef position = copyAttribute(focused, kAXPositionAttribute), extent = copyAttribute(focused, kAXSizeAttribute);
  if (position && extent && AXValueGetValue((AXValueRef)position, kAXValueTypeCGPoint, &origin) && AXValueGetValue((AXValueRef)extent, kAXValueTypeCGSize, &size))
    setRect(env, result, "element", CGRectMake(origin.x, origin.y, size.width, size.height));
  if (position) CFRelease(position);
  if (extent) CFRelease(extent);
  setString(env, result, "selected", textAttribute(focused, kAXSelectedTextAttribute));
  CFTypeRef selection = copyAttribute(focused, kAXSelectedTextRangeAttribute);
  CFRange range;
  if (selection && AXValueGetValue((AXValueRef)selection, kAXValueTypeCFRange, &range)) {
    setString(env, result, "before", textBefore(focused, range.location));
    const CFIndex at = range.location + range.length;
    CGRect box;
    // An empty range has no box in most apps: measure the character before the caret and take its right edge,
    // or the one after it and take its left edge.
    if (boundsFor(focused, at, 0, &box) && box.size.width < 4) setRect(env, result, "caret", CGRectMake(box.origin.x, box.origin.y, 0, box.size.height));
    else if (at > 0 && boundsFor(focused, at - 1, 1, &box)) setRect(env, result, "caret", CGRectMake(CGRectGetMaxX(box), box.origin.y, 0, box.size.height));
    else if (boundsFor(focused, at, 1, &box)) setRect(env, result, "caret", CGRectMake(box.origin.x, box.origin.y, 0, box.size.height));
    CFTypeRef line = copyAttribute(focused, kAXInsertionPointLineNumberAttribute), lineRange = nullptr;
    if (line && AXUIElementCopyParameterizedAttributeValue(focused, kAXRangeForLineParameterizedAttribute, line, &lineRange) == kAXErrorSuccess
        && lineRange && AXValueGetValue((AXValueRef)lineRange, kAXValueTypeCFRange, &range) && range.length > 0
        && boundsFor(focused, range.location, range.length, &box)) setNumber(env, result, "lineRight", CGRectGetMaxX(box));
    if (line) CFRelease(line);
    if (lineRange) CFRelease(lineRange);
  }
  if (selection) CFRelease(selection);
  CFRelease(focused);
  return result;
}
// Whether Jarvis may read carets and paste; `prompt` shows macOS's own dialog once.
static napi_value accessibility(napi_env env, napi_callback_info info) {
  size_t argc = 1; napi_value args[1]; napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  bool prompt = false;
  if (argc == 1) napi_get_value_bool(env, args[0], &prompt);
  NSDictionary *options = @{ (__bridge NSString *)kAXTrustedCheckOptionPrompt: @(prompt) };
  napi_value result; napi_get_boolean(env, AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options), &result); return result;
}
// ⌘V into the app in front, as Typlus does; the text is already on the pasteboard.
static napi_value paste(napi_env env, napi_callback_info info) {
  CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
  for (bool down : (bool[]){ true, false }) {
    CGEventRef event = CGEventCreateKeyboardEvent(source, 9, down); // 9: V
    CGEventSetFlags(event, kCGEventFlagMaskCommand);
    CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
  }
  if (source) CFRelease(source);
  napi_value result; napi_get_boolean(env, AXIsProcessTrusted(), &result); return result;
}
// A plain Return into the app in front, no modifiers: 言字 0.4.3's Enter to send, after the paste.
static napi_value pressReturn(napi_env env, napi_callback_info info) {
  CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
  for (bool down : (bool[]){ true, false }) {
    CGEventRef event = CGEventCreateKeyboardEvent(source, 36, down); // 36: Return
    CGEventSetFlags(event, (CGEventFlags)0);
    CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
  }
  if (source) CFRelease(source);
  return nullptr;
}
static napi_value init(napi_env env, napi_value exports) {
  napi_value fn; napi_create_function(env, "update", NAPI_AUTO_LENGTH, update, nullptr, &fn);
  napi_set_named_property(env, exports, "update", fn);
  napi_create_function(env, "getFrame", NAPI_AUTO_LENGTH, getFrame, nullptr, &fn); napi_set_named_property(env, exports, "getFrame", fn);
  napi_create_function(env, "setFrame", NAPI_AUTO_LENGTH, setFrame, nullptr, &fn); napi_set_named_property(env, exports, "setFrame", fn);
  napi_create_function(env, "screens", NAPI_AUTO_LENGTH, screens, nullptr, &fn); napi_set_named_property(env, exports, "screens", fn);
  napi_create_function(env, "setStationary", NAPI_AUTO_LENGTH, setStationary, nullptr, &fn); napi_set_named_property(env, exports, "setStationary", fn);
  napi_create_function(env, "commandDown", NAPI_AUTO_LENGTH, commandDown, nullptr, &fn); napi_set_named_property(env, exports, "commandDown", fn);
  napi_create_function(env, "leftMouseDown", NAPI_AUTO_LENGTH, leftMouseDown, nullptr, &fn); napi_set_named_property(env, exports, "leftMouseDown", fn);
  napi_create_function(env, "rightOption", NAPI_AUTO_LENGTH, rightOption, nullptr, &fn); napi_set_named_property(env, exports, "rightOption", fn);
  napi_create_function(env, "leftCommand", NAPI_AUTO_LENGTH, leftCommand, nullptr, &fn); napi_set_named_property(env, exports, "leftCommand", fn);
  napi_create_function(env, "leftOption", NAPI_AUTO_LENGTH, leftOption, nullptr, &fn); napi_set_named_property(env, exports, "leftOption", fn);
  napi_create_function(env, "caret", NAPI_AUTO_LENGTH, caret, nullptr, &fn); napi_set_named_property(env, exports, "caret", fn);
  napi_create_function(env, "pasteTarget", NAPI_AUTO_LENGTH, pasteTarget, nullptr, &fn); napi_set_named_property(env, exports, "pasteTarget", fn);
  napi_create_function(env, "accessibility", NAPI_AUTO_LENGTH, accessibility, nullptr, &fn); napi_set_named_property(env, exports, "accessibility", fn);
  napi_create_function(env, "paste", NAPI_AUTO_LENGTH, paste, nullptr, &fn); napi_set_named_property(env, exports, "paste", fn);
  napi_create_function(env, "pressReturn", NAPI_AUTO_LENGTH, pressReturn, nullptr, &fn); napi_set_named_property(env, exports, "pressReturn", fn);
  return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, init)
