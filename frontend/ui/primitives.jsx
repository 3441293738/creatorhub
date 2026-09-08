// shadcn/ui composition, adapted to the existing semantic CSS and DOM forms.
// https://ui.shadcn.com/docs/components/radix/{sheet,tabs,dropdown-menu}
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

export const Tabs = TabsPrimitive;
