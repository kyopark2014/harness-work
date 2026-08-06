import {
  CompositionEvent,
  FormEvent,
  KeyboardEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

interface QueuedMessage {
  id: string;
  text: string;
  files: string[];
}

interface Props {
  disabled?: boolean;
  /** True while waiting for an assistant response (shows stop icon + progress animation). */
  waiting?: boolean;
  queuedMessages?: QueuedMessage[];
  queuePaused?: boolean;
  onRemoveQueued?: (id: string) => void;
  onSteerQueued?: (id: string) => void;
  onResumeQueue?: () => void;
  onStop?: () => void;
  onSend: (text: string, files?: string[]) => void;
}

const MIN_INPUT_HEIGHT = 24;
const MAX_INPUT_HEIGHT = 160;

export function ChatInput({
  disabled,
  waiting = false,
  queuedMessages = [],
  queuePaused = false,
  onRemoveQueued,
  onSteerQueued,
  onResumeQueue,
  onStop,
  onSend,
}: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  const submitAfterCompositionRef = useRef(false);

  function adjustInputHeight() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const next = Math.min(
      Math.max(el.scrollHeight, MIN_INPUT_HEIGHT),
      MAX_INPUT_HEIGHT,
    );
    el.style.height = `${next}px`;
  }

  useLayoutEffect(() => {
    adjustInputHeight();
  }, [value]);

  function submit(textOverride?: string) {
    const text = (textOverride ?? value).trim();
    if (!text || disabled) return;
    onSend(text, []);
    setValue("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey) return;
    // Korean/CJK IME: Enter confirms composition — do not send mid-compose.
    // keyCode 229 covers browsers that omit isComposing on the confirming Enter.
    if (
      e.nativeEvent.isComposing ||
      e.keyCode === 229 ||
      isComposingRef.current
    ) {
      submitAfterCompositionRef.current = true;
      return;
    }
    // Some browsers fire a second Enter after 229; compositionend will submit.
    if (submitAfterCompositionRef.current) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    submit();
  }

  function onCompositionStart() {
    isComposingRef.current = true;
  }

  function onCompositionEnd(e: CompositionEvent<HTMLTextAreaElement>) {
    isComposingRef.current = false;
    if (!submitAfterCompositionRef.current) return;
    submitAfterCompositionRef.current = false;
    // React state can lag behind the DOM after compositionend; use live value.
    submit(e.currentTarget.value);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (isComposingRef.current || submitAfterCompositionRef.current) return;
    submit();
  }

  const canSend = !disabled && value.trim().length > 0;
  const showInputSteer = queuedMessages.length > 0 && canSend;

  return (
    <div className="chat-input-area">
      {queuedMessages.length > 0 && (
        <div
          className={`chat-queue-panel${queuePaused ? " is-paused" : ""}`}
          aria-label="대기 중인 메시지"
        >
          {queuePaused && (
            <div className="chat-queue-header">
              <span className="chat-queue-paused-label">
                Queue paused because you interrupted
              </span>
              <button
                type="button"
                className="chat-queue-resume"
                onClick={() => onResumeQueue?.()}
              >
                Resume
              </button>
            </div>
          )}
          <ul className="chat-queue">
            {queuedMessages.map((item) => {
              const label = item.text.trim() || "메시지";
              return (
                <li key={item.id} className="chat-queue-item">
                  <span className="chat-queue-text" title={label}>
                    {label}
                  </span>
                  <div className="chat-queue-actions">
                    <button
                      type="button"
                      className="chat-queue-steer"
                      title="진행 중인 응답을 멈추고 이 메시지로 전환"
                      aria-label={`이 메시지로 전환: ${label}`}
                      onClick={() => onSteerQueued?.(item.id)}
                    >
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 16 16"
                        aria-hidden="true"
                      >
                        <path
                          d="M5 3.5 2.5 6 5 8.5M2.5 6H10a3.5 3.5 0 0 1 0 7H8"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.4"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                    <button
                      type="button"
                      className="chat-queue-remove"
                      aria-label={`대기 메시지 삭제: ${label}`}
                      onClick={() => onRemoveQueued?.(item.id)}
                    >
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 16 16"
                        aria-hidden="true"
                      >
                        <path
                          d="M5.5 3.5h5M6.5 3.5V2.75A.75.75 0 0 1 7.25 2h1.5a.75.75 0 0 1 .75.75V3.5m2 0V13a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V3.5h8ZM7 6.5v5M9 6.5v5"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <form className="chat-input-wrap" onSubmit={onSubmit}>
        <textarea
          ref={textareaRef}
          className="chat-input"
          rows={1}
          placeholder="메시지를 입력하세요..."
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onCompositionStart={onCompositionStart}
          onCompositionEnd={onCompositionEnd}
        />
        <div className="chat-input-toolbar">
          {showInputSteer && (
            <button
              type="button"
              className="chat-steer-btn"
              aria-label="진행 중인 응답을 멈추지 않고 대기열에 추가"
              title="진행 중인 응답을 멈추지 않고 대기열에 추가"
              disabled={disabled}
              onClick={() => submit()}
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 16 16"
                aria-hidden="true"
              >
                <path
                  d="M5 3.5 2.5 6 5 8.5M2.5 6H10a3.5 3.5 0 0 1 0 7H8"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          {waiting ? (
            <button
              className="chat-send-btn is-waiting"
              type="button"
              aria-label="응답 중지"
              aria-busy="true"
              onClick={() => onStop?.()}
            >
              <span className="chat-send-progress" aria-hidden="true" />
              <span className="chat-send-stop" aria-hidden="true" />
            </button>
          ) : (
            <button
              className="chat-send-btn"
              type="submit"
              aria-label="전송"
              disabled={!canSend}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M8 12.5V3.5M4.5 7 8 3.5 11.5 7"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
