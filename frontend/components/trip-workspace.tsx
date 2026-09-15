"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  REVISION_INSTRUCTION_LIMIT,
  reviseTrip,
  shareTrip,
  undoTrip,
  type RevisionDiff,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  describeFailure,
  TripResultView,
  useTrip,
  type FailureInfo,
} from "@/components/trip-result";

/**
 * 结果面板：把一份行程取回来、画出来，并接上三个操作 —— **改路线 / 撤销 / 分享**。
 *
 * ★ 一条贯穿全局的规则：每次改动都会**换一条 trip_id** ★
 * 后端的修改是「生成新版本」而不是原地改（`plan_service` 落一条新 trip，
 * `parent_trip_id` 指向上一版）：所以改完/撤销之后，面板必须切到新的 trip_id 并重新取回，
 * 而不是把旧对象在本地拼一拼。这样"界面上看到的"与"接口里存的"永远是同一份东西 ——
 * 刷新、分享、再次修改都不会错位。
 *
 * ★ 另一个刻意的选择：操作结果不美化 ★
 * - 改不动时后端会返回 `needs_clarification`（它没听懂），界面把它当成**反问**显示，不当错误；
 * - 撤销到头了后端说「这已经是最早的版本了」，就照原话说，不翻译成"操作失败"；
 * - 取消分享会让旧链接**立刻 404**（AC-9.7），按钮旁边必须写清楚这一点。
 */

/** 改路线那一栏的状态。 */
type RevisionState =
  | { kind: "idle" }
  | { kind: "revised"; diff: RevisionDiff; revisionNo: number }
  | { kind: "clarify"; question: string }
  | { kind: "failed"; failure: FailureInfo };

interface ShareState {
  url: string;
  slug: string;
}

export function TripWorkspace({ tripId }: { tripId: string }) {
  const [currentId, setCurrentId] = useState(tripId);
  const { trip, loading, error, reload } = useTrip(currentId);

  const [instruction, setInstruction] = useState("");
  const [revising, setRevising] = useState(false);
  const [revision, setRevision] = useState<RevisionState>({ kind: "idle" });

  const [undoing, setUndoing] = useState(false);
  const [undoNote, setUndoNote] = useState<string | null>(null);
  const [undoFailure, setUndoFailure] = useState<FailureInfo | null>(null);

  const [sharing, setSharing] = useState(false);
  /** 分享链接由接口给（`ShareOut.url`），这里只记住当前这份行程的链接。 */
  const [share, setShare] = useState<ShareState | null>(null);
  const [shareNotice, setShareNotice] = useState<string | null>(null);
  const [shareFailure, setShareFailure] = useState<FailureInfo | null>(null);
  const [copied, setCopied] = useState(false);

  const resetFeedback = useCallback(() => {
    setRevision({ kind: "idle" });
    setUndoNote(null);
    setUndoFailure(null);
    setShare(null);
    setShareNotice(null);
    setShareFailure(null);
    setCopied(false);
  }, []);

  // 父组件换了一条行程（用户又提交了一次规划）→ 跟着换，并清掉上一条的提示
  useEffect(() => {
    setCurrentId(tripId);
    resetFeedback();
  }, [tripId, resetFeedback]);

  /**
   * 切到另一个版本。
   *
   * 这里只改 id：内容由 `useTrip` 按 id 重新取回来。刻意不在本地用接口返回的
   * `TripOut` 直接 setState —— 那样界面会有两条"真相"（本地对象与服务器上的那份），
   * 一旦有哪个字段没带上就会安静地显示旧数据。
   */
  function switchTo(nextTripId: string) {
    setCurrentId(nextTripId);
    setInstruction("");
    setUndoNote(null);
    setUndoFailure(null);
    // 新版本是另一条 trip：它自己的公开状态、它自己的链接，不能沿用上一版的
    setShare(null);
    setShareNotice(null);
    setShareFailure(null);
    setCopied(false);
  }

  async function handleRevise(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = instruction.trim();
    if (text === "" || revising) return;

    setRevising(true);
    setRevision({ kind: "idle" });
    try {
      const result = await reviseTrip(currentId, text);
      if (result.data.needs_clarification !== null) {
        // 没听懂不是错误：把它当成一次反问，行程保持不动
        setRevision({ kind: "clarify", question: result.data.needs_clarification });
        return;
      }
      setRevision({
        kind: "revised",
        diff: result.data.diff ?? {},
        revisionNo: result.data.revision_no,
      });
      switchTo(result.data.trip_id);
    } catch (cause: unknown) {
      setRevision({ kind: "failed", failure: describeFailure(cause, "没能改成你要的样子") });
    } finally {
      setRevising(false);
    }
  }

  async function handleUndo() {
    if (undoing) return;
    setUndoing(true);
    setUndoNote(null);
    setUndoFailure(null);
    try {
      const result = await undoTrip(currentId);
      // 撤销返回的是**上一版**那份行程，切过去
      setRevision({ kind: "idle" });
      switchTo(result.data.trip_id);
      setUndoNote(`已回到第 ${result.data.revision_no} 版。`);
    } catch (cause: unknown) {
      // 「这已经是最早的版本了」是正常边界，不是故障 —— 直接转述后端的话
      setUndoFailure(describeFailure(cause, "没能撤销这一次修改"));
    } finally {
      setUndoing(false);
    }
  }

  async function handleShare(next: boolean) {
    if (sharing) return;
    setSharing(true);
    setShareFailure(null);
    setShareNotice(null);
    setCopied(false);
    try {
      const result = await shareTrip(currentId, next);
      if (result.data.is_public) {
        setShare({ url: result.data.url, slug: result.data.slug });
        setShareNotice("链接已生成，任何拿到它的人都能看到这套路线。");
      } else {
        setShare(null);
        setShareNotice("已取消分享：这个链接现在就打不开了（后端会直接返回 404）。");
      }
    } catch (cause: unknown) {
      setShareFailure(describeFailure(cause, next ? "没能生成分享链接" : "没能取消分享"));
    } finally {
      setSharing(false);
    }
  }

  async function copyLink(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      // 剪贴板不可用（http、浏览器策略、用户拒绝）时**不假装成功**：
      // 链接就在上面，让用户自己选中复制。
      setCopied(false);
      setShareNotice("这个环境不允许脚本写剪贴板，请手动复制上面的链接。");
    }
  }

  if (loading) {
    return (
      <p
        data-testid="trip-loading"
        className="animate-stage rounded-[10px] border border-line bg-shell/60 px-4 py-3 text-xs leading-relaxed text-ink-soft"
      >
        正在取回完整行程…
      </p>
    );
  }

  if (error !== null || trip === null) {
    const failure = describeFailure(error, "没能取回这次行程");
    return (
      <div
        data-testid="trip-error"
        className="rounded-[10px] border border-coral/40 bg-coral-tint/70 px-4 py-3"
      >
        <FailureBlock failure={failure} />
        <div className="mt-3">
          <Button variant="secondary" size="md" onClick={reload}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <TripResultView trip={trip} />

      {/* ── 改路线 ─────────────────────────────────────────────────────── */}
      <form
        onSubmit={handleRevise}
        className="rounded-[10px] border border-line bg-shell/60 p-4 sm:p-5"
      >
        <label htmlFor="trip-revise" className="block text-sm font-medium text-ink">
          想改哪儿？用一句话说
        </label>
        <p className="mt-1 text-xs leading-relaxed text-ink-soft">
          比如「别去博物馆」「第二天轻松点」「预算改成 500」。修改只走本地重算，
          不联网、不额外花钱。
        </p>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <input
            id="trip-revise"
            name="instruction"
            type="text"
            autoComplete="off"
            maxLength={REVISION_INSTRUCTION_LIMIT}
            value={instruction}
            onChange={(event) => setInstruction(event.currentTarget.value)}
            placeholder="例如：把广州塔换成别的"
            className="min-h-11 w-full rounded-btn border border-line bg-shell px-3 text-base text-ink placeholder:text-ink-faint focus:border-teal"
          />
          <Button
            type="submit"
            loading={revising}
            disabled={instruction.trim() === ""}
            className="sm:w-auto"
          >
            {revising ? "正在改…" : "改路线"}
          </Button>
        </div>

        <div aria-live="polite">
          {revision.kind === "revised" ? (
            <p data-testid="revision-note" className="mt-3 text-xs leading-relaxed text-teal-dark">
              已生成第 {revision.revisionNo} 版。
              {revision.diff.sentence ? ` 这次改动：${revision.diff.sentence}` : " 路线已按你的要求重算。"}
            </p>
          ) : null}
          {revision.kind === "clarify" ? (
            <p
              data-testid="revision-clarify"
              className="mt-3 rounded-[10px] border border-line bg-sand px-3 py-2.5 text-xs leading-relaxed text-ink-soft"
            >
              没听明白这句，「{revision.question}」—— 换成更具体的说法再试一次（例如
              「去掉广州塔」）。
            </p>
          ) : null}
          {revision.kind === "failed" ? (
            <div
              data-testid="revision-error"
              className="mt-3 rounded-[10px] border border-coral/40 bg-coral-tint/70 px-3 py-2.5"
            >
              <FailureBlock failure={revision.failure} />
            </div>
          ) : null}
        </div>

        {/* ── 撤销 ───────────────────────────────────────────────────── */}
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line/70 pt-4">
          <Button variant="secondary" size="md" loading={undoing} onClick={handleUndo}>
            撤销这次修改
          </Button>
          <p className="text-xs leading-relaxed text-ink-faint">
            回到上一版（每一版都完整保留，不会丢）。
          </p>
        </div>
        <div aria-live="polite">
          {undoNote === null ? null : (
            <p data-testid="undo-note" className="mt-2 text-xs text-teal-dark">
              {undoNote}
            </p>
          )}
          {undoFailure === null ? null : (
            <div
              data-testid="undo-error"
              className="mt-2 rounded-[10px] border border-line bg-sand px-3 py-2.5"
            >
              <FailureBlock failure={undoFailure} />
            </div>
          )}
        </div>
      </form>

      {/* ── 分享 ───────────────────────────────────────────────────────── */}
      <div className="rounded-[10px] border border-line bg-shell/60 p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-medium text-ink">分享这套路线</h3>
            <p className="mt-1 text-xs leading-relaxed text-ink-soft">
              生成一个公开链接后，拿到它的人不需要登录就能看到这份行程。
            </p>
          </div>
          <Button
            variant={share === null ? "secondary" : "quiet"}
            size="md"
            loading={sharing}
            onClick={() => handleShare(share === null)}
          >
            {share === null ? "生成公开链接" : "取消分享"}
          </Button>
        </div>

        {share === null ? null : (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <a
              href={share.url}
              target="_blank"
              rel="noreferrer noopener"
              className="min-h-11 inline-flex items-center rounded-btn border border-line bg-sand px-3 text-xs text-teal-dark underline-offset-2 hover:underline"
            >
              {share.url}
            </a>
            <Button
              variant="secondary"
              size="md"
              onClick={() => void copyLink(share.url)}
            >
              {copied ? "已复制" : "复制链接"}
            </Button>
          </div>
        )}

        <div aria-live="polite">
          {shareNotice === null ? null : (
            <p data-testid="share-note" className="mt-2 text-xs leading-relaxed text-ink-soft">
              {shareNotice}
            </p>
          )}
          {shareFailure === null ? null : (
            <div
              data-testid="share-error"
              className="mt-2 rounded-[10px] border border-coral/40 bg-coral-tint/70 px-3 py-2.5"
            >
              <FailureBlock failure={shareFailure} />
            </div>
          )}
        </div>
      </div>

      <p className="text-xs leading-relaxed text-ink-faint">
        分享页只读（打开的人可以看路线，改不了你的这一份）；地图在每套方案卡片里，
        需要配置 NEXT_PUBLIC_AMAP_JS_KEY 才会显示。
      </p>
    </div>
  );
}

/** 失败块：标题 / 说明 / 细节 / 提示，四行都给出来，不吞信息。 */
function FailureBlock({ failure }: { failure: FailureInfo }) {
  return (
    <>
      <p className="text-sm font-semibold text-ink">{failure.title}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-soft">{failure.message}</p>
      {failure.detail === null ? null : (
        <p className="tnum mt-1 text-xs text-ink-soft">{failure.detail}</p>
      )}
      {failure.hint === null ? null : (
        <p className="mt-1 text-xs leading-relaxed text-ink-soft">{failure.hint}</p>
      )}
    </>
  );
}
