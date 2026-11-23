// Shared accessible dialog, menu, tabs, and select primitives built on Radix UI.
import React, { useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as TabsPrimitive from "@radix-ui/react-tabs";

export function Icon({ name, ...props }) {
  return <svg aria-hidden="true" {...props}><use href={`#i-${name}`} /></svg>;
}

export function Sheet({ open, onOpenChange, title, description, children, onReturnFocus, busy = false }) {
  // Retain the real form until Radix finishes its CSS exit, never a cloned form.
  const lastOpen = useRef(null);
  if (open) lastOpen.current = { title, description, children };
  const content = open ? { title, description, children } : lastOpen.current || { title, description, children };
  return <Dialog.Root open={open} onOpenChange={value => { if (!busy) onOpenChange(value); }}>
    <Dialog.Portal>
      <Dialog.Overlay className="wb-sheet-overlay" />
      <Dialog.Content className="wb-sheet" inert={!open || undefined} onCloseAutoFocus={event => {
        event.preventDefault(); onReturnFocus?.();
      }} onInteractOutside={event => event.preventDefault()}
        onEscapeKeyDown={event => {
          // Escape dismisses the innermost existing select/date popup first.
          if (document.querySelector('.wb-sheet .cs-panel,.wb-sheet .dt-panel')) event.preventDefault();
        }}>
        <div className="wb-sheet-heading">
          <div><Dialog.Title className="wb-sheet-title">{content.title}</Dialog.Title>
            <Dialog.Description className="wb-sheet-description">{content.description}</Dialog.Description></div>
          <Dialog.Close className="ghost wb-icon-button" aria-label="关闭面板" disabled={busy}><Icon name="x" /></Dialog.Close>
        </div>
        {content.children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}

function actionIcon(button) {
  const action = button.getAttribute("onclick") || "";
  if (button.classList.contains("danger")) return "trash";
  if (/环境/.test(button.textContent)) return /检查|检测/.test(button.textContent) ? "shield" : "settings";
  if (/toggleCookie/.test(action)) return "cookie";
  if (/fingerprint/i.test(action)) return "fingerprint";
  if (/Proxy/.test(action)) return /test/i.test(action) ? "network" : "globe";
  if (/check.*Env/i.test(action)) return "shield";
  if (/Env|Runtime/.test(action)) return "settings";
  if (/扫码/.test(button.textContent)) return "qr";
  if (/login/i.test(action)) return "login";
  if (/refresh/i.test(action)) return "refresh";
  if (/Hub/.test(action)) return "library";
  if (/Browser/.test(action)) return "external";
  return "user";
}

function actionLabel(button) {
  const text = button.textContent.trim();
  return ({ 数据: "查看内容", 环境: "浏览器环境", 指纹: "设备指纹", 代理: "设置代理", 测代理: "测试代理" })[text] || text;
}

export function ActionMenu({ buttons, label = "更多操作", onOpenChange, triggerLabel = "更多", heading = "账号操作", icon = "more" }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef(null), actionPending = useRef(false);
  return <Menu.Root modal={false} onOpenChange={value => { setOpen(value); if (value) actionPending.current = false; onOpenChange?.(value); }}>
    <Menu.Trigger ref={trigger} className={`ghost sm wb-more ${icon === "plus" ? "wb-add-account" : ""}`} aria-label={label}><Icon name={icon} /><span>{triggerLabel}</span>{icon === "plus" && <Icon name="chevron" className="wb-trigger-chevron" />}</Menu.Trigger>
    <Menu.Portal><Menu.Content className="wb-menu" inert={!open || undefined} sideOffset={6} align="end" collisionPadding={12}
      onCloseAutoFocus={event => { if (actionPending.current) event.preventDefault(); }}>
      <Menu.Label className="wb-menu-label">{heading}</Menu.Label>
      {buttons.map((button, index) => <React.Fragment key={index}>
        {button.classList.contains("danger") && <Menu.Separator className="wb-menu-separator" />}
        <Menu.Item className="wb-menu-item" data-danger={button.classList.contains("danger") || undefined}
          disabled={button.disabled} onSelect={() => {
            // Do not wait for the visual exit to run the real operation. Prevent
            // delayed menu auto-focus from stealing focus from its next dialog.
            actionPending.current = true;
            setTimeout(() => {
              trigger.current?.focus({ preventScroll: true });
              button.click();
              if (button.getAttribute("onclick") === "toggleCookie()") document.getElementById("ck-val")?.focus();
            }, 0);
          }}><Icon name={actionIcon(button)} /><span>{actionLabel(button)}</span></Menu.Item>
      </React.Fragment>)}
    </Menu.Content></Menu.Portal>
  </Menu.Root>;
}

export function SelectBox({
  id,
  value,
  onChange,
  options = [],
  placeholder = "请选择",
  ariaLabel,
  ariaDescribedBy,
  className = "",
  full = false,
  disabled = false,
  hidden = false,
  prefixIcon = null,
}) {
  const [open, setOpen] = useState(false);
  if (hidden) return null;
  const strVal = value === undefined || value === null ? "" : String(value);
  const selected = options.find(opt => String(opt.value) === strVal) || null;
  const displayLabel = selected ? selected.label : placeholder;
  const leadIcon = selected?.icon || prefixIcon;
  return (
    <Menu.Root modal={false} open={open} onOpenChange={v => { if (!disabled) setOpen(v); }}>
      <Menu.Trigger
        id={id}
        type="button"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        className={`mx-select-trg ${full ? "full" : ""} ${!selected ? "is-ph" : ""} ${className}`.trim()}
      >
        <span className="mx-sel-main">
          {selected?.dot && <span className="mx-sel-dot" style={{ background: selected.dot }} aria-hidden="true" />}
          {!selected?.dot && leadIcon && <Icon name={leadIcon} className="mx-sel-ic" />}
          <span className="mx-sel-lbl">{displayLabel}</span>
          {selected?.count !== undefined && selected?.count !== null && (
            <span className="mx-sel-cnt">{selected.count}</span>
          )}
        </span>
        <span className="mx-sel-arr" aria-hidden="true"><Icon name="chevron" /></span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          className="wb-menu mx-select-menu"
          inert={!open || undefined}
          sideOffset={5}
          align="start"
          collisionPadding={10}
          onPointerDown={e => e.stopPropagation()}
          onClick={e => e.stopPropagation()}
        >
          {options.map((opt, idx) => {
            const isSel = String(opt.value) === strVal;
            return (
              <Menu.Item
                key={`${opt.value}-${idx}`}
                className="wb-menu-item mx-select-opt"
                data-selected={isSel || undefined}
                disabled={opt.disabled}
                onSelect={() => {
                  if (!opt.disabled && String(opt.value) !== strVal) {
                    onChange?.(opt.value);
                  }
                }}
              >
                {opt.dot && <span className="mx-sel-dot" style={{ background: opt.dot }} aria-hidden="true" />}
                {!opt.dot && opt.icon && <Icon name={opt.icon} className="mx-sel-opt-ic" />}
                <span className="mx-sel-opt-lbl">{opt.label}</span>
                {opt.count !== undefined && opt.count !== null && (
                  <span className="mx-sel-cnt">{opt.count}</span>
                )}
                <Icon name="check" className="mx-sel-check" />
              </Menu.Item>
            );
          })}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

export const Tabs = TabsPrimitive;
