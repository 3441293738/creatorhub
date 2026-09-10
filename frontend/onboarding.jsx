import React, { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";

const KEY = "creatorhub:onboarding:v1";
const platforms = {
  douyin: { name: "抖音", task: "下载第一条作品", target: "share-download", hint: "复制自己作品的完整分享文案，粘贴到链接下载并提交。完成后打开实际文件。" },
  xhs: { name: "小红书", task: "下载第一篇笔记", target: "share-download", hint: "先完成主站扫码登录，复制有效的完整笔记链接。发布还需单独完成创作者登录。" },
  kuaishou: { name: "快手", task: "下载第一条作品", target: "share-download", hint: "选择快手账号，粘贴完整分享文案，下载完成后检查保存路径。" },
  shipinhao: { name: "视频号", task: "同步自己的作品", target: "hub", hint: "在我的内容选择本账号并同步。仅支持自己的数据；没有作品时空列表是正常的。" },
};
function savedState() {
  try {
    const data = JSON.parse(localStorage.getItem(KEY) || "{}");
    return data && typeof data === "object" ? data : {};
  } catch { return {}; }
}

export function Onboarding({ platform }) {
  const [saved, setSaved] = useState(savedState);
  const [open, setOpen] = useState(() => !savedState().seen);
  const [selected, setSelected] = useState(platforms[platform] ? platform : "douyin");
  const [step, setStep] = useState(0);
  const current = platforms[selected];
  const guideBase = document.documentElement.dataset.guideBase || "https://3441293738.github.io/creatorhub/guide/";
  const demo = document.documentElement.dataset.preview === "true";
  function persist(next) {
    setSaved(next);
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* private mode remains usable */ }
  }
  function changeOpen(value) {
    setOpen(value);
    if (!value) persist({ ...saved, seen: true });
  }
  function navigate(target) {
    const bridge = window.CreatorHubBridge;
    bridge.selectPlatform(selected);
    changeOpen(false);
    bridge.navigate(target, true);
  }
  return <Dialog.Root open={open} onOpenChange={changeOpen}>
    <Dialog.Trigger asChild><button type="button" className="ghost" onClick={() => { setSelected(platforms[platform] ? platform : "douyin"); setStep(0); }}>新手向导</button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="wb-sheet-overlay" />
      <Dialog.Content className="wb-onboarding">
        <div className="wb-onboarding-head"><span>CREATORHUB / GET STARTED</span><Dialog.Close asChild><button className="ghost sm" aria-label="关闭新手向导">关闭</button></Dialog.Close></div>
        <Dialog.Title>跟着三步，完成第一次使用</Dialog.Title>
        <Dialog.Description>{demo ? "当前是示例演示：操作不会登录真实账号或产生真实任务。" : "先选平台、再登录、最后完成一个小任务。不会自动发布或发送任何内容。"}</Dialog.Description>
        <ol className="wb-onboarding-steps" aria-label="上手步骤">{["选择平台", "登录账号", "第一个任务"].map((label, i) => <li key={label}><button className="ghost" aria-current={step === i ? "step" : undefined} onClick={() => setStep(i)}>{i + 1}. {label}</button></li>)}</ol>
        {step === 0 && <div><h3>你想先使用哪个平台？</h3><div className="wb-onboarding-platforms">{Object.entries(platforms).map(([key, value]) => <button key={key} className="ghost" aria-pressed={selected === key} onClick={() => setSelected(key)}><b>{value.name}</b><span>{value.task}</span></button>)}</div><p>不同平台的能力独立。视频号只操作本账号数据，小红书主站与创作者登录分开。</p></div>}
        {step === 1 && <div><h3>登录你的{current.name}账号</h3><ol><li>点击下方按钮，进入平台账号。</li><li>选择“添加账号”，按平台提示扫码或登录。</li><li>回到账号列表检测状态，再从“新手向导”继续。</li></ol><p>{selected === "xhs" ? "先扫码获取主站读取态。发布笔记时，还要完成独立的创作者登录。" : "登录需要本机弹出的浏览器窗口。账号列表为空时，先完成登录。"}</p><button type="button" onClick={() => navigate("accounts")}>前往{current.name}账号页</button></div>}
        {step === 2 && <div><h3>{current.task}</h3><p>{current.hint}</p><button type="button" onClick={() => navigate(current.target)}>前往操作页面</button><p><label className="wb-onboarding-check"><input type="checkbox" checked={saved.completed?.[selected] === true} onChange={e => persist({ ...saved, completed: { ...saved.completed, [selected]: e.target.checked } })} />我已检查结果，标记这条路线完成</label></p><small>这是你的手动学习记录，不代表系统已验证账号或任务状态。记录保存在当前浏览器，换端口或设备后需重新标记。</small></div>}
        <a href={`${guideBase}${selected}/`} target="_blank" rel="noopener noreferrer">打开{current.name}图文教程 ↗</a>
        <div className="wb-onboarding-footer"><button className="ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>上一步</button>{step < 2 ? <button onClick={() => setStep(step + 1)}>下一步</button> : <button onClick={() => changeOpen(false)}>返回工作台</button>}</div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
