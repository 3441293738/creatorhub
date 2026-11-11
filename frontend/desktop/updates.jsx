import React from "react";
import brand from "../../app/brand.json";
export default function Updates({ state, showSettings = true }) {
  return showSettings ? <section className="settings-group"><h2>版本</h2><p>{brand.name} {state.version}</p><p>本地开发验证版 · 更新由本地构建提供</p></section> : null;
}
