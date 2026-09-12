#!/usr/bin/env node
/* 构建前预检: 提取 inject_deploy_arch.py 的全部注入片段 (ADV_GROUP /
   SCHED_GROUP / WORKER_FETCH), 包进官方上下文做语法检查。 */
const fs = require('fs');
const t = fs.readFileSync('/mnt/gpustack/deploy/inject_deploy_arch.py', 'utf8');

function extract(name) {
  const m = t.match(new RegExp(name + ' = \\(\\n([\\s\\S]*?)\\n\\)\\n'));
  if (!m) { console.error('!! ' + name + ' not found'); process.exit(1); }
  const parts = [...m[1].matchAll(/'((?:[^'\\]|\\.)*)'/g)].map(x => x[1]);
  return parts.join('');
}

const adv = extract('ADV_GROUP');
const fetch = extract('WORKER_FETCH').replace(/,$/, '');
const sched = extract('SCHED_GROUP');

/* 高级 tab 上下文: 组件 ne 内, e=intl, n=form, k/Y/z/D 组件 */
const advCtx = 'var e={formatMessage:()=>""},n={getFieldValue:()=>{},setFieldValue:()=>{}},k={Z:{Item:()=>{}}},D={jsx:()=>{},jsxs:()=>{},Fragment:()=>{}},Y={Z:{Input:()=>{}}},z={Z:()=>{}};'
  + 'function t(){return (0,D.jsxs)(D.Fragment,{children:[' + fetch + ',' + adv + '(0,D.jsx)("div",{})]})}';

/* 调度 tab 上下文: 组件 Cn 内, d=form instance, 其余同上 */
const schedCtx = 'var d={getFieldValue:()=>null,setFieldValue:()=>{}},k={Z:{Item:()=>{}}},D={jsx:()=>{},jsxs:()=>{},Fragment:()=>{}},z={Z:()=>{}};'
  + 'function t(){return (0,D.jsxs)(D.Fragment,{children:[' + sched + '(0,D.jsx)("div",{})]})}';

try {
  new Function(advCtx);
  new Function(schedCtx);
  console.log('inject 预检: ADV_GROUP + SCHED_GROUP + WORKER_FETCH 语法 OK (官方上下文)');
} catch (e) {
  console.error('!! 注入片段语法错误: ' + e.message);
  process.exit(1);
}
