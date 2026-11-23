import { useEffect, useLayoutEffect, useRef, useState } from "react";

const MAX_SECONDS = 20;
const MAX_BYTES = 5 * 1024 * 1024;
const FORMATS = ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus"];

type Recording = {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  startedAt: number;
  timer: number;
  send: boolean;
};

function release(stream: MediaStream) {
  stream.getTracks().forEach((track) => track.stop());
}

export default function ChatVoice({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (blob: Blob) => void;
}) {
  const [pending, setPending] = useState(false);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [clip, setClip] = useState<Blob | null>(null);
  const [error, setError] = useState("");
  const active = useRef<Recording | null>(null);
  const permissionRequest = useRef(0);
  const supported = window.isSecureContext && typeof navigator.mediaDevices?.getUserMedia === "function"
    && typeof window.MediaRecorder === "function";

  function stop(send: boolean) {
    const capture = active.current;
    if (!capture || capture.recorder.state === "inactive") return;
    capture.send = send;
    window.clearTimeout(capture.timer);
    capture.recorder.stop();
    release(capture.stream);
  }

  function clearCapture() {
    permissionRequest.current++;
    const capture = active.current;
    active.current = null;
    if (!capture) return;
    window.clearTimeout(capture.timer);
    if (capture.recorder.state !== "inactive") capture.recorder.stop();
    release(capture.stream);
  }

  useLayoutEffect(() => clearCapture, []);
  useEffect(() => {
    if (!recording) return;
    const clock = window.setInterval(() => {
      const capture = active.current;
      if (capture) setElapsed(Math.min(MAX_SECONDS, Math.floor((performance.now() - capture.startedAt) / 1000)));
    }, 250);
    return () => window.clearInterval(clock);
  }, [recording]);

  function submit(blob: Blob) {
    if (!blob.size || blob.size > MAX_BYTES) {
      setError(blob.size ? "每段錄音最多 5 MiB，請縮短錄音後重試。" : "錄音沒有聲音，請重新錄音。");
      return;
    }
    setClip(null);
    onSend(blob);
  }

  async function start() {
    setError("");
    if (!supported) return;
    const mimeType = FORMATS.find((format) => MediaRecorder.isTypeSupported(format));
    if (!mimeType) {
      setError("此瀏覽器沒有支援的錄音格式。請改用文字輸入。");
      return;
    }
    let stream: MediaStream | undefined;
    setPending(true);
    const request = ++permissionRequest.current;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (permissionRequest.current !== request) {
        release(stream);
        return;
      }
      const recorder = new MediaRecorder(stream, { mimeType });
      const capture: Recording = {
        recorder, stream, chunks: [], startedAt: performance.now(), timer: 0, send: false,
      };
      active.current = capture;
      recorder.ondataavailable = (event) => {
        if (event.data.size) capture.chunks.push(event.data);
      };
      recorder.onerror = () => {
        if (active.current !== capture) return;
        clearCapture();
        setRecording(false);
        setError("錄音中斷，請重新錄音或改用文字輸入。");
      };
      recorder.onstop = () => {
        release(capture.stream);
        if (active.current !== capture) return;
        window.clearTimeout(capture.timer);
        active.current = null;
        setRecording(false);
        const blob = new Blob(capture.chunks, { type: recorder.mimeType || mimeType });
        if (capture.send) submit(blob);
        else setClip(blob);
      };
      recorder.start();
      // Stop slightly before 20 s to allow the final encoded audio frame.
      capture.timer = window.setTimeout(() => stop(false), MAX_SECONDS * 1000 - 100);
      setElapsed(0);
      setRecording(true);
    } catch (failure) {
      if (stream) release(stream);
      if (permissionRequest.current !== request) return;
      active.current = null;
      const denied = failure instanceof DOMException && failure.name === "NotAllowedError";
      setError(denied ? "麥克風未獲授權。請開放權限或改用文字輸入。" : "無法開啟麥克風，請改用文字輸入。");
    } finally {
      if (permissionRequest.current === request) setPending(false);
    }
  }

  function cancel() {
    clearCapture();
    setPending(false);
    setRecording(false);
    setClip(null);
    setError("");
  }

  return (
    <div className="voice-controls">
      {supported ? (
        <>
          {!recording && !clip && (
            <button type="button" disabled={disabled || pending} onClick={start}>
              {pending ? "等待麥克風權限…" : "開始錄音"}
            </button>
          )}
          {(recording || clip || pending) && (
            <>
              {recording && <span aria-label={`錄音中，${elapsed} 秒`}><span className="recording-dot" aria-hidden="true" /> {elapsed} 秒</span>}
              {clip && <span role="status">錄音已停止，可送出或取消。</span>}
              {(recording || clip) && (
                <button type="button" disabled={disabled} onClick={() => clip ? submit(clip) : stop(true)}>
                  {clip ? "送出錄音" : "停止並送出"}
                </button>
              )}
              <button type="button" onClick={cancel}>取消錄音</button>
            </>
          )}
          <small>送出的錄音會傳給 Gemini 轉寫，每段最多 20 秒。</small>
        </>
      ) : (
        <small>麥克風需要 HTTPS 或 localhost 與支援錄音的瀏覽器，可使用文字輸入。</small>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
