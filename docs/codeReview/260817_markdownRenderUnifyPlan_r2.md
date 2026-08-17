Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: markdownRenderUnifyPlan v1.1 复审。

# markdownRenderUnifyPlan v1.1 复审

复审对象：`docs/plan/markdownRenderUnifyPlan.md`（v1.1，未实施）。
上一轮审核：`docs/codeReview/260817_markdownRenderUnifyPlan.md`（针对 v1.0）。
核对代码：`webApp/frontend/js/chatView.js`（v1.16，1283 行）、`webApp/frontend/js/fileExplorer.js`（v1.1）、`webApp/frontend/styles.css`（v1.14，640 行）、`webApp/frontend/index.html`。代码自上一轮审核以来未变（版本号一致），v1.0 审核引用的行号与事实全部仍然成立。

---

## 复审结论

**可以实施。** v1.1 完整消化了上一轮全部 2 个 P0 与 4 个 P1，5 条 P2 也全部吸收；本轮复核 D1–D5 的描述与真实代码逐处比对，未发现事实性错误，无必须再改的 P0/P1。仅余 2 条 P2 级措辞瑕疵（见下），不阻塞实施，建议实施时顺手按注执行即可。

重点核对结果：

- **D4 挂载点（原 P0-1）**：四处挂载点逐一在代码中定位确认——① `case 'completed'`（L803-811，flushAndCollapseThinking 在 L807、goIdle 在 L809，顺序与方案一致）；② `handleStreamError` stopped 分支（settleRunningCardsOnStop 在 markInterrupted 之前，方案「之后、markInterrupted 之前或之后均可」成立）；③ 其它 errorType 内联分支（canInline 判定与 appendInlineErrorBlock 均在，emptyMessage/无 step 回退 errorBar 的排除说明属实）；④ `stop()`（flushLivePaint 在进 stopping 后、markInterrupted 前，方案挂载成立）。「明确不挂」清单（confirmationRequired / goIdle / onStreamClosed / pendingConfirmationExists / confirmationMismatch）与代码语义全部吻合。
- **CSS 清单（原 P0-2）**：styles.css 行号核实——L270 `:first-child`、L271-279 六条（pre / code / pre code / table / th,td / blockquote）、全局 `code` L45、`.preview-markdown` L475，与方案引用完全一致。v1.1 比我上轮建议多做了一点正确的事：明确全局 `code`（L45）**保留**给非 markdown 区域（设置页、确认框确实在用裸 code），避免误删。
- **脚本顺序**：index.html 现状为 marked → dompurify → highlight →（无 markdown.js）→ fileExplorer → chatView，方案 §3 的插入位置描述成立。
- **T1.6 grep 基线**：复核确认 `marked.setOptions` 全仓仅 chatView L76 一处、`marked.parse` 仅 chatView L83 与 fileExplorer L199 两处、`hljs.highlight` 仅 fileExplorer L163 一处——方案的「属预期两处」基线描述准确。

## 上一轮问题消化表

| 编号 | 结论 | 说明 |
|------|------|------|
| P0-1 挂载点 | **已修** | D4 改为四处挂载点表格 + 「明确不挂」清单 + renderFinal 契约（前置 flush、只碰 contentEl、空值守卫），与代码逐处吻合。 |
| P0-2 CSS 清单 | **已修** | D2 补了 pre/code/pre code.hljs 四条覆盖代码块，T2.4 落为可执行删改清单（删 L271-279 六条、留 L270、L45 全局 code 保留），比上轮建议更完整。 |
| P1-3 降级 | **已修** | D1 明确「缺 marked 降级 textContent」并标注为行为变更/附带 bug fix；S1.3 新增 S8 登记。 |
| P1-4 hljs 唯一路径 | **已修** | D1 拍板唯一路径：textContent → hljs.highlight → sanitize → innerHTML + hljs class，明文禁止 highlightElement，与 fileExplorer 现网模式一致。 |
| P1-5 Phase1 预览 breaks 变更 | **已修** | T1.6 写明 `hljs.highlight` 两处属预期；Phase 1 头部声明「本阶段不是观感零变化」，预览单换行按空行分段为预期变更。 |
| P1-6 attach 中间态 | **已修** | S1 加例外注释（attach 中间态允许前半高亮后半不高亮），验收表新增 H（attach 回放终态收敛），TODO 加 T3.5。 |
| P2-1 D3 措辞 | **已修** | 改为「v1.7 前每 token 同步 parse 已证明不可行；rAF 合并后每帧一次全文 parse 可接受」。 |
| P2-2 预览 padding | **已修** | `.preview-markdown.markdown-body { padding: 14px 18px }` 已写入 D2 覆盖块。 |
| P2-3 img 验证 | **已修** | 验收 G 与 T2.5 均落为勾选项（含大图 .md 不撑破 .file-preview-body）。 |
| P2-4 版本锁定 | **已修** | T2.1 拍板锁定 github-markdown-css 5.9.0，文件头写版本。CSP 备注未加，本期无 CSP，无关紧要。 |
| P2-5 回滚 | **已修** | 回滚步骤 2 明确要求恢复 setOptions 或复制函数内 per-call 传 breaks，并指出「只改回函数不恢复 breaks，聊天会黏行」。 |

## 本轮新问题

无必须再改项。以下两条为 P2 级建议，实施时按注执行即可，不需要再出一版方案：

1. **D4 挂载点表 #4 的顺序注释可再精确半句**（P2）。`stop()` 真实行序是 `flushLivePaint`（L1086）→ `markInterrupted`（L1087）→ `settleRunningCardsOnStop`（L1088），方案表格只写「flushLivePaint 之后、markInterrupted 之前或之后均可」。renderFinal 只碰 contentEl，挂在 flushLivePaint 之后任意位置都正确，功能无影响；建议实施注释写成「flushLivePaint 之后、settleRunningCardsOnStop 前后均可」，与 #2 挂载点的表述对齐，免得实施者对着代码找「markInterrupted 是最后一行」产生疑惑。
2. **验收 I 可补一条对称检查**（P2）。验收 I 查「completed 后 contentEl 无二次 hljs 套娃」，建议同场景顺手确认 stopped/stop() 路径同样只高亮一次（D4 挂载点 #2/#4 与 goIdle 双保险不挂的交叉验证），一行手测即可，不需要改方案文本。

## 同意实施的决策

- **D1（只抽同步渲染函数）——仍成立。** API 形态、流式调度留在 chatView 的判断不变；实现要点现已无歧义（hljs 唯一路径、textContent 降级、绝不传 bodyEl）。
- **D2（vendor github-markdown-css light + 本地覆盖）——仍成立。** 5.9.0 已锁定，link 置 styles.css 之前、容器级 + pre/code/hljs 四级覆盖、T2.4 删改清单完整，接入即回归的风险已消除。
- **D3（继续全文 marked）——仍成立。** 措辞修正后理由真实，不换增量库的判断不变。
- **D4（live 不高亮、终态/历史/预览高亮）——仍成立。** 挂载点表格与代码逐处吻合，「flushLivePaint 永不 highlight + renderFinal ×4」契约可执行；「明确不挂」清单排除了误挂中间帧的所有入口。
- **D5（聊天 breaks:true / 预览 breaks:false）——仍成立。** Phase 1 的预览排版变更已声明为预期，回滚步骤已补齐 breaks 恢复要求。
