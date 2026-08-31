"use client";

import { useCallback, useRef, useState } from "react";

/**
 * 表单在途防重：包裹异步提交，保证同一时刻只有一个提交在进行。
 *
 * 背景：弹窗的提交按钮此前没有在途状态，双击或连按回车会把同一份数据
 * 提交多次（创建出重复记录）。`Button` 组件本身已支持 `loading`（会自动
 * disabled），这里只负责产生这个状态并拦截重入。
 *
 * 用法：
 *   const [saving, submit] = useSubmitGuard();
 *   <Button loading={saving} type="submit">保存</Button>
 *   await submit(async () => { await api.post(...); });
 */
export function useSubmitGuard(): [
  boolean,
  (fn: () => Promise<void>) => Promise<void>,
] {
  const [saving, setSaving] = useState(false);
  const inFlight = useRef(false);

  const submit = useCallback(async (fn: () => Promise<void>) => {
    if (inFlight.current) return; // 已有提交在途：直接忽略本次触发
    inFlight.current = true;
    setSaving(true);
    try {
      await fn();
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }, []);

  return [saving, submit];
}
