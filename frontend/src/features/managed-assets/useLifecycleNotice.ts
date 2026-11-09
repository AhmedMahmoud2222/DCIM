import { useCallback, useEffect, useRef, useState } from "react";

export interface LifecycleNotice {
  id: number;
  message: string;
}

export function useLifecycleNotice() {
  const [notice, setNotice] = useState<LifecycleNotice | null>(null);
  const [isFocused, setIsFocused] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const sequence = useRef(0);
  const noticeRef = useRef<HTMLParagraphElement>(null);

  const showNotice = useCallback((message: string) => {
    sequence.current += 1;
    setIsFocused(false);
    setIsHovered(false);
    setNotice({ id: sequence.current, message });
  }, []);
  const clearNotice = useCallback(() => {
    setIsFocused(false);
    setIsHovered(false);
    setNotice(null);
  }, []);

  useEffect(() => {
    if (!notice) return;
    noticeRef.current?.focus();
    if (isFocused || isHovered) return;
    // The eight-second reading window starts after keyboard and pointer interaction ends.
    const timeout = window.setTimeout(() => {
      setNotice((current) => current?.id === notice.id ? null : current);
    }, 8_000);
    return () => window.clearTimeout(timeout);
  }, [isFocused, isHovered, notice]);

  const noticeEventHandlers = {
    onFocus: () => setIsFocused(true),
    onBlur: () => setIsFocused(false),
    onPointerEnter: () => setIsHovered(true),
    onPointerLeave: () => setIsHovered(false),
  };

  return { notice, noticeRef, noticeEventHandlers, showNotice, clearNotice };
}
