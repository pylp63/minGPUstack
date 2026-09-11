#!/usr/bin/env node
/* 构建前预检: 提取 inject_deploy_arch.py 的 FIELD_GROUP, 包进官方上下文做语法检查. */
const fs = require('fs');
const t = fs.readFileSync('/mnt/gpustack/deploy/inject_deploy_arch.py', 'utf8');
const m = t.match(/FIELD_GROUP = \(\n([\s\S]*?)\n\)\n/);
if (!m) { console.error('!! FIELD_GROUP not found'); process.exit(1); }
const parts = [...m[1].matchAll(/'((?:[^'\\]|\\.)*)'/g)].map(x => x[1]);
const group = parts.join('');
const wrapper = 'var e={formatMessage:()=>""},n={setFieldValue:()=>{}},k={Z:{Item:()=>{}}},D={jsx:()=>{},jsxs:()=>{},Fragment:()=>{}},Y={Z:{Input:()=>{}}},z={Z:()=>{}};'
  + 'function t(){return (0,D.jsxs)(D.Fragment,{children:[' + group + '(0,D.jsx)("div",{})]})}';
try {
  new Function(wrapper);
  console.log('inject 预检: FIELD_GROUP 语法 OK (官方上下文)');
} catch (e) {
  console.error('!! 注入片段语法错误: ' + e.message);
  process.exit(1);
}
