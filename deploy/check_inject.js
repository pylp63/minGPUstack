#!/usr/bin/env node
/* 构建前预检: 提取 inject_deploy_arch.py 的全部注入片段 (ADV_GROUP /
   RANK_BOXES / PD_SWITCH / WORKER_FETCH), 包进官方上下文做语法检查。 */
const fs = require('fs');
const path = require('path');
const DEPLOY_DIR = path.dirname(__filename);
const t = fs.readFileSync(path.join(DEPLOY_DIR, 'inject_deploy_arch.py'), 'utf8');

function extract(name) {
  const m = t.match(new RegExp(name + ' = \\(\\n([\\s\\S]*?)\\n\\)\\n'));
  if (!m) { console.error('!! ' + name + ' not found'); process.exit(1); }
  const parts = [...m[1].matchAll(/'((?:[^'\\]|\\.)*)'/g)].map(x => x[1]);
  return parts.join('');
}

const adv = extract('ADV_GROUP');
const fetch = extract('WORKER_FETCH').replace(/,$/, '');
const rank = extract('RANK_BOXES');
const pdsw = extract('PD_SWITCH');

/* 高级 tab 上下文: 组件 ne 内, e=intl, n=form, k/Y/z/D 组件 */
const advCtx = 'var e={formatMessage:()=>""},n={getFieldValue:()=>{},setFieldValue:()=>{}},k={Z:{Item:()=>{}}},D={jsx:()=>{},jsxs:()=>{},Fragment:()=>{}},Y={Z:{Input:()=>{}}},z={Z:()=>{}};'
  + 'function t(){return (0,D.jsxs)(D.Fragment,{children:[' + fetch + ',' + adv + '(0,D.jsx)("div",{})]})}';

/* 调度 tab 上下文: Cn 组件内, d=form instance。
   PD_SWITCH 内嵌 RANK_BOXES 与 GPU items 占位 — gpu_items_src 在构建时
   拼入, 这里用等价占位表达式模拟 (一个 jsx item) 验语法。 */
/* PD_SWITCH 在 Python 端拼接 gpu_items_src (运行时变量), JS 预检拿不到
   完整串 — 其语法由构建期 node --check 完整产物兜底; 这里验证 RANK_BOXES。 */
const rankCtx = 'var d={getFieldValue:()=>null,setFieldValue:()=>{}},k={Z:{Item:()=>{}}},D={jsx:()=>{},jsxs:()=>{},Fragment:()=>{}},z={Z:()=>{}};'
  + 'function t(){return (0,D.jsxs)(D.Fragment,{children:[' + rank + '(0,D.jsx)("div",{})]})}';

try {
  new Function(advCtx);
  new Function(rankCtx);
  console.log('inject 预检: ADV_GROUP + RANK_BOXES + WORKER_FETCH 语法 OK (官方上下文)');
} catch (e) {
  console.error('!! 注入片段语法错误: ' + e.message);
  process.exit(1);
}

/* inject_backends_images.py 的镜像行片段 (IIFE) 语法自检 */
try {
  const imgRow = '(function(){var fi=a.framework_images;if(!fi||!Object.keys(fi).length){return null}var rows=[];Object.keys(fi).forEach(function(fw){var arr=Array.isArray(fi[fw])?fi[fw]:[fi[fw]];arr.forEach(function(img){if(img){rows.push(fw+": "+img)}})});if(!rows.length){return null}return rows.length})();';
  new Function('a', 'return ' + imgRow);
  console.log('inject 预检: 镜像行片段语法 OK');
} catch (e) {
  console.error('!! 镜像行片段语法错误: ' + e.message);
  process.exit(1);
}

/* inject_workers_page.py 的 CPU/GPU 类型列片段语法自检 */
try {
  const wt = fs.readFileSync(path.join(DEPLOY_DIR, 'inject_workers_page.py'), 'utf8');
  const m = wt.match(/TYPE_COL = \(\n([\s\S]*?)\n\)/);
  if (!m) { console.error('!! TYPE_COL not found in inject_workers_page.py'); process.exit(1); }
  const parts = [...m[1].matchAll(/'((?:[^'\\]|\\.)*)'/g)].map(x => x[1]);
  const typeCol = parts.join('');
  // 列数组元素上下文: [..., TYPE_COL, {IP列}] — 两边都要有数组邻居
  new Function('ae', 'return [' + typeCol + '{title:"IP"}]');
  console.log('inject 预检: workers 类型列片段语法 OK');
} catch (e) {
  console.error('!! workers 类型列片段语法错误: ' + e.message);
  process.exit(1);
}
