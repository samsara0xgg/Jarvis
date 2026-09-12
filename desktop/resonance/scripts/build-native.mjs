import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
if (process.platform !== 'darwin') { console.log('Native material: macOS only; neutral CSS fallback enabled.'); process.exit(0); }
const candidates = [process.env.NODE_INCLUDE, path.resolve(path.dirname(process.execPath), '../include/node'), '/opt/homebrew/include/node', '/usr/local/include/node'].filter(Boolean);
const include = candidates.find(p => existsSync(path.join(p, 'node_api.h')));
if (!include) throw new Error('Node C headers missing. Install Node with headers or set NODE_INCLUDE.');
mkdirSync('dist-native', { recursive: true });
execFileSync('clang++', ['-std=c++17', '-fobjc-arc', '-shared', '-undefined', 'dynamic_lookup', '-framework', 'Cocoa', '-framework', 'QuartzCore', '-I', include, 'native/material.mm', '-o', 'dist-native/material.node'], { stdio: 'inherit' });
