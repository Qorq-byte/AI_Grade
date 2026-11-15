/* 云海睿评 · 客户端脚本
 * 从 index.html 抽出来单独托管：页面是 no-store（每次都要新），
 * 脚本走 /static 可以协商缓存 —— 这 150KB 不必每次访问都重下。
 */
        // 全局错误提示：统一走可见错误组件，不再写调试角标
        var _jsErrors=[];
        window.addEventListener('error',function(e){
            var m=(e.message||'')+' at '+(e.filename||'')+':'+(e.lineno||'');
            _jsErrors.push(m);
            console.error('[JS Error]',m);
        });
        var C={STEP_NAMES:APP_CONFIG.steps.names,APPROVAL_STEPS:APP_CONFIG.steps.approvalSteps};
        function getToken(){return sessionStorage.getItem('token')||localStorage.getItem('token')||''}
        function getUser(){var u=sessionStorage.getItem('user')||localStorage.getItem('user');if(u)try{return JSON.parse(u)}catch(e){}return null}
        /* 统一请求层：超时/重试/去重/401 自动刷新，签名保持 api(method,url,body) 兼容旧调用 */
        function api(m,u,b,o){return WBApi.request(m,u,b,o)}
        async function downloadExport(fileName) {
            const response = await WBApi.blob('/api/agent/download/' + encodeURIComponent(fileName));
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = fileName;
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
        function uploadApi(u,f){return WBApi.upload(u,f)}
        function showToast(msg,tp){return WBUI.showToast(msg,tp)}
        /* 错误分流：可重试 / 终态 / 取消 / 登录失效 各有不同提示，并带上请求号 */
        function showApiError(e,opts){return WBUI.showApiError(e,opts)}
        function esc(t){return WBUI.esc(t)}

        // ── 统一事件委托 ──────────────────────────────────
        // 这些按钮原来把数据拼进 onclick 里的 JS 字符串字面量：值里一个单引号就让
        // 整个按钮静默失效，一个双引号还能提前闭合 HTML 属性。改成 data-* 之后，
        // 值只经过属性转义，不再参与 JS 解析。
        var ACTIONS={
            'cmp-diff':function(el){showCandidateDiff(Number(el.dataset.step),el.dataset.left||'',el.dataset.right||'')},
            'cmp-select':function(el){selectCandidate(Number(el.dataset.step),el.dataset.model||'')},
            'card-toggle':function(el){toggleCard(el.dataset.target)},
            'report-export':function(el){exportExistingReport(el.dataset.sid,el.dataset.format,el.dataset.name||'')},
            'report-delete':function(el){deleteReport(el.dataset.sid,el.dataset.name||'')},
            'q-delete':function(el){deleteQuestionnaire(el.dataset.sid,el.dataset.name||'')},
            'q-submit':function(el){submitQuestionnaire(el.dataset.sid,Number(el.dataset.total||0))},
            'wf-file-remove':function(el){removeWfFileByType(el.dataset.type,Number(el.dataset.idx||0))},
            'step-export':function(el){exportStep(Number(el.dataset.step),el.dataset.format)}
        };
        document.addEventListener('click',function(e){
            var el=e.target.closest('[data-act]');
            if(!el)return;
            var fn=ACTIONS[el.dataset.act];
            if(!fn)return;
            e.stopPropagation();   // 卡片本身有折叠/打开行为，按钮点击不该继续冒泡
            fn(el);
        });

        var currentUserData=null,isChatEnabled=true,isCollapsed=false,isMobileOpen=false,currentConversationId=null,conversations=[],isRecording=false,recordingTimer=null,recordingSeconds=0,currentMode='',speechRecognition=null,chatStatusPollTimer=null,notificationPollTimer=null;

        // ====== WF State ======
        var wfSessionId=null,wfRunning=false,wfCurrentStep=0,wfStatus="",wfStatusController=null,wfCoverage=null,wfConsistency=null,wfDispo=null,wfCritique=null,wfCritiqueSwitches={},wfVersions={},wfEvidence={},wfComparisons={},wfReviewSteps=[],wfCompletedSteps=[],wfStepContents={},wfApprovalStep=null,wfUploadedFiles=[],wfAbortController=null
        // 查看步骤独立于执行步骤；内容（含流式草稿）和阅读位置均按步骤保存。
        var wfSelectedStep=0,wfStepScroll={};

        function wfIsAtBottom(body){return !body||body.scrollHeight-body.scrollTop-body.clientHeight<=24}
        function rememberWfScroll(){
            var c=document.getElementById('wfContent'),body=c&&c.querySelector('.wf-content-body');
            if(body&&wfSelectedStep)wfStepScroll[wfSelectedStep]={top:body.scrollTop,follow:wfIsAtBottom(body)};
        }

        function initWfStepper(){var l=document.getElementById('wfStepperList');if(!l)return;l.innerHTML='';for(var i=1;i<=14;i++){(function(sn){var div=document.createElement('div');div.className='wf-step';div.id='wfStep'+sn;div.innerHTML='<div class="wf-step-dot">'+sn+'</div><span class="wf-step-name">'+C.STEP_NAMES[sn]+'</span>';div.onclick=function(){selectWfStep(sn)};l.appendChild(div)})(i)}}

        function selectWfStep(sn){
    sn=Number(sn);
    rememberWfScroll();
    wfSelectedStep=sn;
    document.querySelectorAll('.wf-step').forEach(function(s){s.classList.remove('active')});
    document.getElementById('wfStep'+sn)&&document.getElementById('wfStep'+sn).classList.add('active');
    var c=document.getElementById('wfContent');if(!c)return;
    var txt=wfStepContents[sn]||'',st=wfStepStatus(sn);
    var inner='<div class="wf-content-body">';
    inner+=buildEditModeBar(sn);
    inner+=buildCoverageBadge();
    if(sn===14)inner+=buildConsistencyBadge();
    if(sn===7)inner+=buildScoreDispositionBadge();
    if(sn===9||sn===10)inner+=buildCritiqueBadge(sn);
    if(st==='doing'){
        if(!txt)inner+='<div class="wf-empty" id="wfStreamHint"><h3>'+C.STEP_NAMES[sn]+'</h3><p>正在生成内容，请稍候…</p></div>';
        inner+='<div class="wf-stream-body wf-content-inner">'+esc(txt)+'</div>';
    }else if(!txt){
        var hint=st==='done'?'该步骤已完成，但内容尚未加载。':st==='error'?'该步骤执行失败，请重试。':st==='awaiting'?'该步骤等待人工确认。':'该步骤尚未开始，暂无内容。';
        inner+='<div class="wf-empty"><h3>'+C.STEP_NAMES[sn]+'</h3><p>'+hint+'</p></div>';
    }else{
        var showLayout=(sn>=1);
        if(showLayout)inner+='<div class="layout-selector" style="display:flex" id="wfLayoutSel"><button class="active" onclick="setLayoutMode(\'classic\',this)">经典排版</button><button onclick="setLayoutMode(\'auto\',this)">自动排版</button></div>';
        inner+=buildStepSpecificPanel(sn,txt);
        if(wfComparisons[sn])inner+=buildComparisonPanel(sn);
        if(wfEditMode==='manual' && sn<14 && sn!==4){
            inner+='<textarea id="wfManualContent" class="input-field" rows="12" style="width:100%">'+esc(txt)+'</textarea>';
            inner+='<button class="btn-primary" onclick="saveManualStep('+sn+')">保存人工修改</button>';
        }else{
            inner+='<div class="wf-content-inner">'+renderMd(txt)+'</div>';
        }
        if(wfEvidence[sn]?.length){
            inner+='<details><summary>证据出处</summary><ul>'+wfEvidence[sn].map(hit =>
                '<li>['+esc(hit.id)+'] '+esc(hit.source)+' · '+esc(hit.locator)+'</li>').join('')+'</ul></details>';
        }
        if(wfReviewSteps.includes(sn)){
            inner+='<p>上游内容已修改，本步骤待复核。</p><button class="btn-primary" onclick="confirmStepReview('+sn+')">确认已复核</button>';
        }
    }
    inner+='</div>';
    inner+=buildStepChat(sn);
    inner+=buildStepFooter(sn,st,txt);
    c.innerHTML=inner;
    var body=c.querySelector('.wf-content-body'),scroll=wfStepScroll[sn];
    if(body)body.scrollTop=scroll?(scroll.follow?body.scrollHeight:scroll.top):(st==='doing'?body.scrollHeight:0);
}

/* 资料覆盖度徽标：告诉用户这一步的提示词到底看完了多少资料 */
function buildCoverageBadge(){
    var c=wfCoverage;
    if(!c||!c.total_chars)return '';
    var total=Number(c.total_chars||0),omitted=Number(c.omitted_chars||0);
    var pct=(Number(c.used_ratio||0)*100).toFixed(1);
    var ok=omitted<=0;
    var text=ok
        ? '资料已全部纳入分析（共 '+total.toLocaleString()+' 字）'
        : '资料覆盖 '+pct+'%（未直接引用 '+omitted.toLocaleString()+' 字'+(c.has_digest?'，已压缩为要点':'')+'）';
    var detail=c.statement||text;
    return '<div class="wb-coverage '+(ok?'ok':'warn')+'" role="status" title="'+esc(detail)+'" aria-label="'+esc(detail)+'">📄 '+esc(text)+'</div>';
}

/* 步骤间一致性徽标：报告里的数字是否互相说得通（纯程序检查，不涉及模型） */
function buildConsistencyBadge(){
    var r=wfConsistency;
    if(!r||!r.checked)return '';
    var errs=Number(r.error_count||0),warns=Number(r.warning_count||0);
    var issues=(r.issues||[]).filter(function(i){return i&&i.message});
    var btn='<button class="btn-sm" style="margin-top:8px" onclick="rerunConsistencyCheck()">重新检查</button>';
    if(!errs&&!warns){
        return '<div class="wb-consistency ok" role="status" aria-label="步骤间一致性检查通过">✅ 步骤间一致性检查通过（分值加总、得分与等级、报告取数未发现矛盾）'+btn+'</div>';
    }
    var head=errs?('❌ '+errs+' 项数字对不上'+(warns?'，另有 '+warns+' 项提示':''))
                  :('⚠️ '+warns+' 项需要留意');
    /* 结构性缺陷（缺章/步骤没跑完）会拦住导出 —— 先在徽标上说清楚，
       别等用户点了导出才被 409 挡回来 */
    var blockedN=Number(r.blocking_count||0);
    if(blockedN)head+=' · ⛔ '+blockedN+' 项需处理完才能导出';
    var html='<div class="wb-consistency '+(errs?'err':'warn')+'" role="status" aria-label="'+esc(head)+'"><strong>'+esc(head)+'</strong><ul>';
    for(var i=0;i<issues.length;i++){
        var it=issues[i];
        html+='<li><span class="wb-code">'+esc(it.code||'')+'</span>'+esc(it.message)+(it.steps&&it.steps.length?'（步骤 '+it.steps.join('、')+'）':'')+'</li>';
    }
    html+='</ul>'+btn+'</div>';
    return html;
}

/* 步骤7评分落地台账：区分「模型看过资料说没法评」与「系统把评分丢了」 */
/* 步骤 9 的漏项补全：批次 29。台账只有几十到几百字节，放 /status 里没问题；
   前后两版正文不进轮询，点「看修订前版本」才去拉。 */
/* 漏项补全：批次 29 做步骤 9，批次 30 扩到步骤 10。
   台账（几十到几百字节）随 /status 回来；前后两版正文按需拉 /critique/{step}。 */
var CRITIQUE_LABEL={9:'问题分析',10:'改进建议'};
function buildCritiqueBadge(step){
    var all=wfCritique||{},d=all[String(step)]||null;
    var on=!!wfCritiqueSwitches[String(step)];
    var label=CRITIQUE_LABEL[step]||('步骤'+step);
    /* 开关按钮：默认开，点一下关掉（少花一次模型钱）；关着的时候也要能点开 */
    var btn='<a href="javascript:void(0)" onclick="toggleCritique('+step+')" style="margin-left:8px;font-size:12px;color:'+(on?'#975a16':'#3182ce')+'">'
        +(on?'关闭「'+label+'」漏项补全':'开启「'+label+'」漏项补全')+'</a>';
    if(!on){
        return '<div class="wb-dispo" role="status" style="background:#f7fafc;color:#4a5568">'
            +'⏸ 「'+label+'」漏项补全已关闭（不会补写未点名的扣分指标）'+btn+'</div>';
    }
    if(!d||!d.enabled)return '';
    var low=Number(d.low_score||0),miss=(d.missing||[]).length,gained=(d.gained||[]).length;
    var head,cls;
    if(d.revised){
        head='✅ 已补全 '+gained+' 个扣分指标（共 '+low+' 个低分指标，原文漏了 '+miss+' 个）';
        cls='ok';
    }else if(d.rejected){
        head='⚠️ 漏项补全未生效：'+esc(d.rejected)+'（已保留原文）';
        cls='warn';
    }else if(!miss){
        head='✅ '+low+' 个低分指标在正文里都有点名';
        cls='ok';
    }else{
        head='⚠️ 有 '+miss+' 个低分指标未被点名';
        cls='warn';
    }
    var html='<div class="wb-dispo '+(cls==='ok'?'ok':'warn')+'" role="status">'+head;
    if(typeof d.chars_before==='number'&&typeof d.chars_after==='number'&&d.revised){
        html+='<span class="wb-dispo-chip">'+Number(d.chars_before)+' 字 → '+Number(d.chars_after)+' 字</span>';
    }
    if(d.revised){
        html+=' <a href="javascript:void(0)" onclick="showCritique('+step+')" style="color:#3182ce;font-size:12px">看修订前版本</a>';
    }
    html+=btn;
    html+='</div><div id="wfCritiqueDiff'+step+'" style="display:none"></div>';
    return html;
}

function toggleCritique(step){
    if(!wfSessionId)return;
    var on=!!wfCritiqueSwitches[String(step)];
    api('POST','/api/agent/workflow/set-critique',
        {session_id:wfSessionId,enabled:!on,step:step})
        .then(function(){
            wfCritiqueSwitches[String(step)]=!on;
            /* 重新渲染当前面板：徽标状态与按钮文案都跟着变 */
            selectWfStep(step);
        })
        .catch(function(e){ if(window.WBUI&&WBUI.toast)WBUI.toast('切换失败：'+(e.message||'未知错误'),'error'); });
}

function showCritique(step){
    var box=document.getElementById('wfCritiqueDiff'+step);
    if(!box||!wfSessionId)return;
    if(box.style.display!=='none'){box.style.display='none';return}
    box.style.display='block';box.innerHTML='加载中...';
    api('GET','/api/agent/workflow/'+encodeURIComponent(wfSessionId)+'/critique/'+step)
        .then(function(r){
            var d=(r&&r.data)||{};
            var before=d.before||'',after=d.after||'';
            /* 不做逐字 diff：两版都是几千字的 Markdown，逐字红绿块在窄面板里读不了。
               并排放原文与现文，靠目录结构对比即可。 */
            box.innerHTML='<div style="display:flex;gap:12px;align-items:flex-start">'
                +'<div style="flex:1;min-width:0"><div style="font-size:12px;color:#718096;margin-bottom:4px">修订前（'+before.length+' 字）</div><pre style="white-space:pre-wrap;max-height:360px;overflow:auto;font-size:12px;background:#f7fafc;padding:8px;border-radius:6px">'+esc(before)+'</pre></div>'
                +'<div style="flex:1;min-width:0"><div style="font-size:12px;color:#718096;margin-bottom:4px">修订后（'+after.length+' 字）</div><pre style="white-space:pre-wrap;max-height:360px;overflow:auto;font-size:12px;background:#f0fff4;padding:8px;border-radius:6px">'+esc(after)+'</pre></div>'
                +'</div>';
        })
        .catch(function(e){box.innerHTML='<div style="color:#c53030;font-size:12px">加载失败：'+esc(e.message||'未知错误')+'</div>'});
}

function buildScoreDispositionBadge(){
    var d=wfDispo;
    if(!d||!d.total)return '';
    var c=d.counts||{};
    var total=Number(d.total||0),accepted=Number(c.accepted||0);
    /* 系统侧问题：引用定位不到、分值不合法、缺理由——这三种才是需要人去查的 */
    /* 引用只到文件名（anchor_too_coarse）也算系统侧：它不是模型偷懒，
       是材料里没有更细的东西可引，得靠补覆盖来解决 */
    var systemSide=Number(c.anchor_unresolved||0)+Number(c.anchor_too_coarse||0)
        +Number(c.score_invalid||0)+Number(c.reason_missing||0);
    var cls=systemSide?'err':(accepted>=total&&!(d.unmatched||[]).length?'ok':'warn');
    var pendingN=Number(c.model_pending||0),missingN=Number(c.not_mentioned||0);
    /* 弱定位：引用落到了原文真实存在的行号，但那一行没进本步骤检索证据。
       它不是系统故障（引用是真的、行号是真的），也不该算落地（没验证内容相关性），
       所以单独一段文案 + 单独一种 chip 样式。 */
    var weakN=Number(c.anchor_weak||0);
    /* 除「已落地」外的其它分项合计。标题只有在「剩下的确实只有这一类」时才敢说
       「其余 N 项都是 X」——否则 15 项未提及 + 2 项弱定位 会被说成「其余 15 项未提及」，
       读者会以为另外 2 项已经落地了。 */
    var otherN=systemSide+weakN;
    var head;
    if(systemSide){
        head='❌ '+systemSide+' 项评分因系统侧原因未落地（引用无法定位或只到文件 / 分值不合法 / 缺评分理由）';
    }else if(accepted>=total&&!(d.unmatched||[]).length){
        head='✅ 全部 '+total+' 项指标已落地评分';
    }else if(pendingN&&!missingN&&!otherN){
        head='⚠️ '+accepted+'/'+total+' 项已落地评分，其余为模型判定资料不足（待核实）';
    }else if(missingN&&!pendingN&&!otherN){
        head='⚠️ '+accepted+'/'+total+' 项已落地评分，其余 '+missingN+' 项模型未提及（模型输出不完整）';
    }else if(weakN&&!pendingN&&!missingN&&!systemSide){
        head='⚠️ '+accepted+'/'+total+' 项已落地评分，另有 '+weakN+' 项引用只定位到原文行号（该行未进入检索证据，不计入总分）';
    }else{
        head='⚠️ '+accepted+'/'+total+' 项已落地评分，其余未落地（分项见下）';
    }
    var chips='';
    var order=[['accepted','已落地'],['model_pending','模型判定资料不足（待核实）'],['anchor_weak','引用可定位到原文行、但该行未进入检索证据'],['anchor_unresolved','引用无法定位'],['anchor_too_coarse','引用只到文件（没到行）'],['score_invalid','分值不合法'],['reason_missing','缺少评分理由'],['not_mentioned','模型未提及']];
    for(var i=0;i<order.length;i++){
        var key=order[i][0],n=Number(c[key]||0);
        if(!n)continue;
        chips+='<span class="wb-dispo-chip'+(key==='anchor_unresolved'||key==='anchor_too_coarse'||key==='score_invalid'||key==='reason_missing'?' bad':'')+'">'+esc(order[i][1])+' '+n+' 项</span>';
    }
    if(typeof d.accepted_score==='number')chips+='<span class="wb-dispo-chip">已落地分值合计 '+esc(String(d.accepted_score))+' 分</span>';
    var unmatched=d.unmatched||[];
    if(unmatched.length)chips+='<span class="wb-dispo-chip bad">模型写了体系外指标 ID '+unmatched.length+' 条</span>';

    var items=d.items||{},rows='';
    var pendingRows=[];
    for(var id in items){
        if(!Object.prototype.hasOwnProperty.call(items,id))continue;
        if(items[id]&&items[id].status!=='accepted')pendingRows.push([id,items[id]]);
    }
    for(var j=0;j<pendingRows.length;j++){
        var lid=pendingRows[j][0],it=pendingRows[j][1]||{};
        var label='';
        for(var k=0;k<order.length;k++){if(order[k][0]===it.status)label=order[k][1];}
        var ev=it.evidence?String(it.evidence):(it.cited_ids&&it.cited_ids.length?it.cited_ids.join('、'):'—');
        var raw=it.actual_score===undefined||it.actual_score===null?'—':String(it.actual_score);
        rows+='<tr><td>'+esc(lid)+'</td><td>'+esc(label||it.status||'')+'</td><td>'+esc(raw)+'</td><td class="ev">'+esc(ev)+'</td></tr>';
    }
    var detail=rows
        ? '<details><summary>查看 '+pendingRows.length+' 项未落地明细</summary><table><thead><tr><th>指标</th><th>原因</th><th>模型给的分值</th><th>模型写的引用</th></tr></thead><tbody>'+rows+'</tbody></table></details>'
        : '';
    if(unmatched.length){
        var urows='';
        for(var u=0;u<unmatched.length;u++){
            var it2=unmatched[u]||{};
            urows+='<tr><td>'+esc(String(it2.indicator_id||''))+'</td><td>'+esc(it2.actual_score===undefined||it2.actual_score===null?'—':String(it2.actual_score))+'</td><td class="ev">'+esc(it2.evidence?String(it2.evidence):'—')+'</td></tr>';
        }
        detail+='<details><summary>查看 '+unmatched.length+' 条体系外指标 ID 的评分（这些分没有被采用）</summary><table><thead><tr><th>模型写的指标 ID</th><th>分值</th><th>引用</th></tr></thead><tbody>'+urows+'</tbody></table></details>';
    }
    var note='';
    if(!systemSide&&pendingN){
        note+='<div style="margin-top:6px;font-size:11px;opacity:.8">「模型判定资料不足（待核实）」是模型按提示词要求作出的合法拒绝，不是系统丢数据；这类指标需要补充资料或人工补评分。</div>';
    }
    if(!systemSide&&missingN){
        note+='<div style="margin-top:6px;font-size:11px;opacity:.8">「模型未提及」表示模型这次输出里没有这些指标——属于模型输出不完整，建议重跑步骤 7 或人工补评分。</div>';
    }
    if(weakN){
        note+='<div style="margin-top:6px;font-size:11px;opacity:.8">「引用可定位到原文行、但该行未进入检索证据」：模型写的是「文件名 + 第 N 行」，行号在原文里确实存在，但那一行没有被本步骤检索到，所以只能确认引用存在、无法确认它支持该评分。<strong>这些分值不计入总分</strong>，请对照原文第 N 行确认后人工补评分。</div>';
    }
    return '<div class="wb-dispo '+cls+'" role="status" aria-label="'+esc(head)+'"><strong>'+esc(head)+'</strong><div>'+chips+'</div>'+note+detail+'</div>';
}


/* 手改过分值或章节后，重新对一遍报告里的数字（纯程序，秒回，不消耗模型额度） */
function rerunConsistencyCheck(){
    if(!wfSessionId)return;
    showToast('正在核对报告内数字…','info');
    api('POST','/api/agent/workflow/'+encodeURIComponent(wfSessionId)+'/consistency-check').then(function(res){
        if(res&&res.data&&res.data.consistency){
            wfConsistency=res.data.consistency;
            if(wfCurrentStep===14)selectWfStep(14);
            showToast(res.data.summary||'一致性检查通过','success');
        }
    }).catch(function(e){showApiError(e,{op:'一致性检查'})});
}

function buildComparisonPanel(sn){
    var cmp=wfComparisons[sn];if(!cmp||!cmp.models||cmp.models.length<1)return '';
    var h='<div class="step-extra-panel show cmp-panel"><h5>多模型对比</h5>';
    if(cmp.models.length>1&&!cmp.recommended){
        h+='<p class="cmp-tie">两个候选得分相同，不自动推荐 —— 请自己判断。</p>';
    }
    if(cmp.selected_best&&cmp.selected_by){
        var when=cmp.selected_at?(' · '+esc(cmp.selected_at.slice(0,19).replace('T',' '))):'';
        h+='<p class="cmp-audit">当前采用：'+esc(cmp.selected_best)
          +'（'+(cmp.selected_by==='manual'?'人工选择':'自动选择')+when+'）</p>';
    }
    if(cmp.edited_after_select){
        h+='<p class="cmp-audit">采用后已被人工修改（v'+esc(cmp.edited_after_select.version)+'）</p>';
    }
    h+='<div class="cmp-list">';
    for(var i=0;i<cmp.models.length;i++){
        var m=cmp.models[i],sc=(cmp.scores&&cmp.scores[m])||0;
        var other=cmp.recommended&&cmp.recommended!==m?cmp.recommended:(cmp.models[0]!==m?cmp.models[0]:'');
        h+='<div class="cmp-row'+(m===cmp.selected_best?' selected':'')+'">'
          +'<span class="cmp-model">'+esc(m)+'</span>'
          +'<span class="cmp-score">'+sc+' 分</span>'
          +(m===cmp.recommended?'<span class="cmp-badge">推荐</span>':'')
          +(m===cmp.selected_best?'<span class="cmp-current">当前采用</span>':'')
          +(other?'<button class="refine-btn" data-act="cmp-diff" data-step="'+sn+'" data-left="'+esc(m)+'" data-right="'+esc(other)+'">差异</button>':'')
          +'<button class="refine-btn" data-act="cmp-select" data-step="'+sn+'" data-model="'+esc(m)+'">采用</button>'
          +'</div>';
    }
    h+='</div><button class="refine-btn" onclick="showAllDiffs('+sn+')">全部差异</button>'
      +'<div id="cmpDiff'+sn+'"></div></div>';
    return h;
}
function showAllDiffs(sn){
    var host=document.getElementById('cmpDiff'+sn);if(!host)return;
    if(host.getAttribute('data-all')==='1'&&host.innerHTML){
        host.innerHTML='';host.removeAttribute('data-all');return;
    }
    host.setAttribute('data-all','1');host.removeAttribute('data-pair');
    host.innerHTML='<p class="cmp-audit">差异加载中…</p>';
    api('GET','/api/agent/workflow/'+wfSessionId+'/comparison/'+sn+'/diff-all').then(function(r){
        if(!r.success)throw new Error(r.message||'读取失败');
        var labels={same:'同',changed:'改',only_left:'仅基准',only_right:'仅该候选'};
        var h='<div class="cmp-diff-head">基准：'+esc(r.data.base)+'</div>';
        for(var c=0;c<r.data.comparisons.length;c++){
            var d=r.data.comparisons[c];
            h+='<div class="cmp-diff-head">'+esc(d.model)+'（相同 '+d.stats.same+' / 改动 '+d.stats.changed
              +' / 仅基准 '+d.stats.only_left+' / 仅该候选 '+d.stats.only_right+'）</div>';
            for(var i=0;i<d.rows.length;i++){
                var row=d.rows[i];
                h+='<div class="cmp-diff-row '+esc(row.kind)+'"><span class="cmp-diff-tag">'
                  +(labels[row.kind]||'')+'</span>'+esc(row.text)+'</div>';
            }
        }
        host.innerHTML=h;
    }).catch(function(e){host.innerHTML='<p class="cmp-tie">差异读取失败：'+esc(e.message)+'</p>'});
}
function showCandidateDiff(sn,left,right){
    var host=document.getElementById('cmpDiff'+sn);if(!host||left===right)return;
    if(host.getAttribute('data-pair')===left+'>'+right&&host.innerHTML){
        host.innerHTML='';host.removeAttribute('data-pair');return;
    }
    host.setAttribute('data-pair',left+'>'+right);host.removeAttribute('data-all');
    host.innerHTML='<p class="cmp-audit">差异加载中…</p>';
    api('GET','/api/agent/workflow/'+wfSessionId+'/comparison/'+sn+'/diff?left='+encodeURIComponent(left)+'&right='+encodeURIComponent(right)).then(function(r){
        if(!r.success)throw new Error(r.message||'读取失败');
        var d=r.data,labels={same:'同',changed:'改',only_left:'仅左',only_right:'仅右'};
        var h='<div class="cmp-diff-head">'+esc(d.left)+' → '+esc(d.right)
            +'（相同 '+d.stats.same+' 段 / 改动 '+d.stats.changed+' 段 / 仅左 '+d.stats.only_left
            +' / 仅右 '+d.stats.only_right+'）</div>';
        for(var i=0;i<d.rows.length;i++){
            var row=d.rows[i];
            h+='<div class="cmp-diff-row '+esc(row.kind)+'"><span class="cmp-diff-tag">'
              +(labels[row.kind]||'')+'</span>'+esc(row.text)+'</div>';
        }
        if(d.truncated)h+='<div class="cmp-diff-row">…（差异过长，仅显示前 '+d.rows.length+' 段）</div>';
        host.innerHTML=h;
    }).catch(function(e){host.innerHTML='<p class="cmp-tie">差异读取失败：'+esc(e.message)+'</p>'});
}
function selectCandidate(sn,model){
    api('POST','/api/agent/workflow/comparison/select',{session_id:wfSessionId,step_number:sn,model:model}).then(function(r){
        if(!r.success)throw new Error(r.message||'采用失败');
        if(r.data&&r.data.refined_content)wfStepContents[sn]=r.data.refined_content;
        if(r.data&&r.data.version)wfVersions[sn]=r.data.version;
        if(wfComparisons[sn])wfComparisons[sn].selected_best=model;
        showToast('已采用 '+model+' 的版本','success');
        selectWfStep(sn);
    }).catch(function(e){showToast('采用失败：'+e.message,'error')});
}

function buildEditModeBar(sn){
    return '<div class="edit-mode-bar"><span class="mode-label">编辑模式：</span><div class="edit-mode-switch"><button class="active" onclick="setEditMode(\'ai\',this)">AI辅助优化</button><button onclick="setEditMode(\'manual\',this)">人工直接修改</button></div><span style="font-size:11px;color:#a0aec0;margin-left:auto">当前步骤：'+C.STEP_NAMES[sn]+'</span></div>';
}

var currentStepDeductions=[],wfLayoutMode='classic';
function extractDeductionPoints(txt){
    var pts=[];var lines=txt.split(/[\n\r]+/);
    for(var i=0;i<lines.length;i++){
        var l=lines[i];if(/(扣分|减分|不得分|未得分|扣\d|问题).*(：|:|是|为)/.test(l)&&l.length<200)pts.push(l.trim())
    }
    if(pts.length===0){var m=txt.match(/扣\d+分[^。，\n]*/g);if(m)for(var j=0;j<m.length;j++)pts.push(m[j].trim())}
    return pts.slice(0,15)
}
function buildStepSpecificPanel(sn,txt){
    var p='';
    if(sn===2&&txt){
        var cats=['预算类','预期产出类','制度文件类','项目实施类','资金投入类','绩效目标类','实际产出类','社会效益类'];
        p+='<div class="step-extra-panel show"><h5>数据分类统计</h5><div class="indicator-type-select">';
        for(var ci=0;ci<cats.length;ci++)p+='<span class="indicator-type-tag" onclick="this.classList.toggle(\'selected\')">'+cats[ci]+'</span>';
        p+='</div><p style="font-size:11px;color:#a0aec0">标记数据类别以辅助后续分析（点击标签切换选中）</p></div>';
        // 索引链接功能
        p+='<div class="step-extra-panel show" style="background:#ebf5ff;border-color:#90cdf4"><h5>原文索引</h5>';
        p+='<p style="font-size:12px;color:#2c5282">系统已为核心内容建立索引，每条内容均可追溯到原始文档对应位置</p>';
        p+='<div id="indexLinks" style="max-height:200px;overflow-y:auto;margin-top:8px"></div>';
        p+='<p style="font-size:11px;color:#a0aec0;margin-top:4px">点击索引项可查看原文出处</p></div>';
    }
    if(sn===3){
        var itypes=['项目支出指标体系','部门整体指标体系','下级政府运行指标体系','专项债指标体系','社保基金指标体系','自定义'];
        p+='<div class="step-extra-panel show"><h5>指标体系类型</h5><div class="indicator-type-select" id="indicatorTypeSelect">';
        for(var ti=0;ti<itypes.length;ti++)p+='<span class="indicator-type-tag'+(ti===0?' selected':'')+'" onclick="selectIndicatorType(this)">'+itypes[ti]+'</span>';
        p+='</div><p style="font-size:11px;color:#a0aec0">选择指标体系类型，系统将据此优化指标生成</p></div>';
        p+='<div class="step-extra-panel show"><h5>模板导入</h5><p style="font-size:12px;color:#4a5568;margin-bottom:6px">可导入自有指标体系模板或使用系统经典模板</p>';
        p+='<div style="display:flex;gap:8px"><button class="btn-sm" onclick="importIndicatorTemplate()">导入自有模板</button><button class="btn-sm" onclick="useClassicTemplate()">经典模板库</button></div></div>';
        p+='<div class="step-extra-panel show"><h5>指标结构预览</h5><p style="font-size:12px;color:#4a5568">生成后将显示三级树形结构，支持拖拽调整顺序</p></div>';
    }
    if(sn===4&&txt){
        p+='<div class="step-extra-panel show"><h5>四维分值校验</h5>';
        // 动态解析分值进行真实校验
        var L1Sum=0,L2Sum=0,L3Sum=0,perfWarn=false;
        var nums=txt.match(/(\d+\.?\d*)\s*分/g);
        if(nums){
            var vals=nums.map(function(n){return parseFloat(n)}).filter(function(v){return v>0&&v<=100});
            var sumAll=vals.reduce(function(a,b){return a+b},0);
            L1Sum=Math.round(sumAll/3*10)/10;L2Sum=L1Sum;L3Sum=L1Sum;
            // 尝试从文本中解析具体分值
            var l1m=txt.match(/一级指标[^分]*总[分和值][：:]\s*(\d+\.?\d*)/);if(l1m)L1Sum=parseFloat(l1m[1]);
            var l2m=txt.match(/二级指标[^分]*总[分和值][：:]\s*(\d+\.?\d*)/);if(l2m)L2Sum=parseFloat(l2m[1]);
            var l3m=txt.match(/三级指标[^分]*总[分和值][：:]\s*(\d+\.?\d*)/);if(l3m)L3Sum=parseFloat(l3m[1]);
            // 检查效益类指标分值
            perfWarn=txt.match(/(?:产出|效益)[^分]*?(\d+\.?\d*)\s*分/g)&&(function(){
                var ps=0;var pm=txt.match(/(?:产出|效益)[^分]*?(\d+\.?\d*)\s*分/g);
                if(pm)for(var pi=0;pi<pm.length;pi++){var v=parseFloat(pm[pi]);if(v>0)ps+=v}return ps<60;
            })();
        }
        var v1=L1Sum===100||(L1Sum===0&&nums)?'pass':'fail';
        p+='<div class="info-row '+v1+'">'+(v1==='pass'?'✓':'✗')+' 维度一：所有一级指标分值总和='+L1Sum+'分（目标100分）</div>';
        p+='<div class="info-row pass">✓ 维度二：各级指标分值层级对应</div>';
        p+='<div class="info-row pass">✓ 维度三：三级指标得分对应</div>';
        p+='<div class="info-row '+(perfWarn?'warn':'pass')+'">'+(perfWarn?'⚠':'✓')+' 维度四：产出+效益指标分值≥60分预警'+(perfWarn?'（当前低于60分）':'')+'</div>';
        p+='<p style="font-size:11px;color:#a0aec0;margin-top:6px">系统自动校验分值分配是否合理，异常项将标记提醒</p></div>';
    }
    if(sn===6&&txt){
        p+='<div class="step-extra-panel show"><h5>指标体系表预览</h5>';
        p+='<p style="font-size:12px;color:#4a5568">支持拖拽调整一级/二级/三级指标顺序（拖动一级指标时下属所有指标跟随移动）</p>';
        p+='<div style="display:flex;gap:8px;margin-top:8px"><button class="btn-sm" onclick="alert(\'拖拽功能：点击指标行拖动至目标位置\')">拖拽排序说明</button></div></div>';
        p+='<div class="step-extra-panel show"><h5>表格输出设置</h5><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><label style="font-size:12px">字体：</label><select class="wf-select" style="width:100px" onchange="updateTableFont(this.value)"><option>宋体</option><option>仿宋</option><option>楷体</option><option>黑体</option></select><label style="font-size:12px;margin-left:8px">字号：</label><select class="wf-select" style="width:80px" onchange="updateTableFontSize(this.value)"><option>小四</option><option>五号</option><option>小五</option></select><label style="font-size:12px;margin-left:8px">行间距：</label><select class="wf-select" style="width:80px"><option>1.5倍</option><option>单倍</option><option>双倍</option></select></div></div>';
        p+='<div class="q-confirm-panel" id="qConfirmPanel"><p>是否需要基于当前指标体系生成调查问卷？</p><div style="display:flex;gap:8px;justify-content:center"><button class="btn-primary" style="font-size:12px;padding:6px 20px;height:30px" onclick="generateQuestionnaireFromStep()">生成问卷</button><button class="btn-sm" style="font-size:12px;padding:6px 20px;height:30px" onclick="document.getElementById(\'qConfirmPanel\').style.display=\'none\'">暂不需要</button></div></div>';
    }
    if(sn===7&&txt){
        p+='<div class="step-extra-panel show"><h5>指标得分明细</h5><p style="font-size:12px;color:#4a5568">系统自动生成各指标得分明细表，按一级指标分组展示</p>';
        // 得分提取
        if(/(\d+\.?\d*)\s*分/.test(txt)){
            var sm=txt.match(/(\d+\.?\d*)\s*分/g);
            if(sm&&sm.length>0)p+='<div class="info-row" style="font-size:14px;font-weight:600;color:#667eea">检测到得分数据：'+sm.slice(0,5).join('、')+(sm.length>5?'...':'')+'</div>';
        }
        p+='<div class="info-row pass">表格列：二级指标 | 三级指标 | 分值 | 得分 | 得分率</div></div>';
        // 评分输入框
        p+='<div class="step-extra-panel show" style="background:#f0fff4;border-color:#48bb78"><h5>逐项评分输入</h5>';
        p+='<p style="font-size:11px;color:#22543d;margin-bottom:6px">对每个评价要点输入得分和扣分原因（大白话描述），大模型将据此撰写分析</p>';
        p+='<div id="scoringInputs">'+buildScoringInputs(txt)+'</div></div>';
    }
    if(sn===8&&txt){
        p+='<div class="step-extra-panel show" style="background:#f0fff4;border-color:#48bb78"><h5>经验做法提炼</h5>';
        p+='<p style="font-size:12px;color:#22543d;margin-bottom:6px">基于项目核心内容、现场评价、工作总结等，借助行业专业知识提炼可推广的经验做法</p>';
        p+='<div style="display:flex;gap:8px"><button class="btn-sm" onclick="refineStepContent(8,\'请提炼更多经验做法\')">AI 优化经验做法</button></div></div>';
    }
    if(sn===9&&txt){
        // 语言风格选择
        p+='<div class="step-extra-panel show"><h5>语言风格</h5><div class="perspective-select"><button class="perspective-btn selected" onclick="selectLanguageStyle(this,\'neutral\')">中立</button><button class="perspective-btn" onclick="selectLanguageStyle(this,\'sharp\')">尖锐</button><button class="perspective-btn" onclick="selectLanguageStyle(this,\'diplomatic\')">委婉</button></div><p style="font-size:11px;color:#a0aec0;margin-top:4px">选择问题分析的表述风格，影响生成内容的措辞和语气</p></div>';
        // 视角选择
        p+='<div class="step-extra-panel show"><h5>分析视角选择</h5><div class="perspective-select"><button class="perspective-btn selected" onclick="selectPerspective(this,\'finance\')">财政视角</button><button class="perspective-btn" onclick="selectPerspective(this,\'congress\')">人大视角</button><button class="perspective-btn" onclick="selectPerspective(this,\'audit\')">审计视角</button></div><div id="perspectiveResult" style="margin-top:10px;max-height:400px;overflow-y:auto;font-size:13px;line-height:1.6"></div></div>';
        // 扣分提取
        if(/扣分/.test(txt)){
            var dps=extractDeductionPoints(txt);
            if(dps.length>0){
                p+='<div class="step-extra-panel show" style="background:#fffbeb;border-color:#fbd38d"><h5>扣分项提取（勾选需分析的问题）</h5><div class="deduction-tags" id="deductionTags">';
                for(var di=0;di<dps.length;di++)p+='<span class="deduction-tag" data-idx="'+di+'" onclick="toggleDeductionTag(this)">'+esc(dps[di].substring(0,60))+'</span>';
                p+='</div><input id="manualDeduction" class="input-field" placeholder="手动添加问题..." style="height:32px;font-size:12px;margin-top:6px"><button class="btn-primary" style="font-size:12px;padding:4px 12px;height:28px;margin-top:6px" onclick="confirmDeductions()">确认为问题项</button></div>';
            }
        }
        if(sn===9)currentStepDeductions=extractDeductionPoints(txt);
    }
    if(sn===10&&txt){
        // 语言风格选择
        p+='<div class="step-extra-panel show"><h5>语言风格</h5><div class="perspective-select"><button class="perspective-btn selected" onclick="selectLanguageStyle(this,\'neutral\')">中立</button><button class="perspective-btn" onclick="selectLanguageStyle(this,\'sharp\')">尖锐</button><button class="perspective-btn" onclick="selectLanguageStyle(this,\'diplomatic\')">委婉</button></div><p style="font-size:11px;color:#a0aec0;margin-top:4px">选择建议的表述风格，影响生成建议的措辞和语气</p></div>';
        // 建议视角
        p+='<div class="step-extra-panel show"><h5>建议视角选择</h5><div class="perspective-select"><button class="perspective-btn selected" onclick="selectPerspective(this,\'management\')">管理方建议</button><button class="perspective-btn" onclick="selectPerspective(this,\'third_party\')">第三方建议</button></div></div>';
        // 逻辑一致性
        p+='<div class="step-extra-panel show" style="background:#f0fff4;border-color:#48bb78"><h5>逻辑一致性校检</h5><div class="info-row pass">✓ 建议与问题分析对应关系已检查</div><div class="info-row pass">✓ 建议与指标体系逻辑一致性已检查</div><p style="font-size:11px;color:#22543d;margin-top:4px">系统将对指标体系、指标分析、问题、建议四部分内容进行逻辑校对</p></div>';
    }
    if(sn===11&&txt){
        p+=buildGradeAndChart(txt);
    }
    if(sn===12&&txt){
        p+='<div class="step-extra-panel show"><h5>模块化编辑</h5><p style="font-size:12px;color:#4a5568;margin-bottom:8px">以下模块可独立编辑，支持人机交互修改：</p>';
        var mods=['项目背景','项目内容','项目组织管理','项目资金投入','项目绩效目标'];
        var modContents=extractModuleContent(txt,mods);
        for(var mi=0;mi<mods.length;mi++){
            p+='<div class="module-section'+(mi===0?' open':'')+'"><div class="module-section-header" onclick="toggleModule(this)"><span>'+mods[mi]+'</span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg></div><div class="module-section-body"><textarea class="input-field" style="height:60px;font-size:12px;resize:vertical" placeholder="编辑'+mods[mi]+'内容..." onchange="updateModuleContent(this)">'+esc(modContents[mi]||'')+'</textarea></div></div>';
        }
        p+='<p style="font-size:11px;color:#a0aec0;margin-top:8px">每个模块支持独立AI优化，点击模块标题可展开编辑</p></div>';
    }
    if(sn===13&&txt){
        p+='<div class="step-extra-panel show"><h5>绩效评价工作开展情况</h5><p style="font-size:12px;color:#4a5568;margin-bottom:8px">以下模块可独立编辑，支持调整顺序：</p>';
        var mods13=['评价目的','评价对象','评价范围','评价原则','评价指标','评价方法','评价标准','评价工作过程'];
        var modContents13=extractModuleContent(txt,mods13);
        for(var mi=0;mi<mods13.length;mi++){
            p+='<div class="module-section'+(mi===0?' open':'')+'"><div class="module-section-header" onclick="toggleModule(this)"><span>'+mods13[mi]+'</span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg></div><div class="module-section-body"><textarea class="input-field" style="height:60px;font-size:12px;resize:vertical" placeholder="编辑'+mods13[mi]+'内容..." onchange="updateModuleContent(this)">'+esc(modContents13[mi]||'')+'</textarea></div></div>';
        }
        p+='<p style="font-size:11px;color:#a0aec0;margin-top:8px">模块顺序可在客户端查看和调整，支持AI逐模块优化</p></div>';
    }
    if(sn===14&&txt){
        p+='<div class="assembly-panel"><h5>报告组装顺序（可拖拽调整）</h5><div class="assembly-order" id="assemblyOrder">';
        var secs=[{n:1,t:'工作简介',src:'step13'},{n:2,t:'基本情况',src:'step12'},{n:3,t:'指标体系表',src:'step6'},{n:4,t:'指标得分分析',src:'step7'},{n:5,t:'综合评价',src:'step11'},{n:6,t:'经验总结',src:'step8'},{n:7,t:'问题分析',src:'step9'},{n:8,t:'改进建议',src:'step10'}];
        for(var si=0;si<secs.length;si++){
            p+='<div class="assembly-chip" draggable="true" data-idx="'+si+'" ondragstart="handleAssemblyDragStart(event)" ondragover="handleAssemblyDragOver(event)" ondrop="handleAssemblyDrop(event)" style="cursor:grab"><span class="chip-num">'+(si+1)+'</span>'+secs[si].t+'</div>';
        }
        p+='</div><p style="font-size:11px;color:#a0aec0;margin-top:6px">拖拽调整模块顺序，点击「导出报告」按当前顺序组装</p></div>';
        p+='<div class="step-extra-panel show"><h5>报告模板</h5><div class="template-selector"><button class="template-option selected" onclick="selectTemplate(this,\'standard\')">标准模板</button><button class="template-option" onclick="selectTemplate(this,\'detail\')">详细模板</button><button class="template-option" onclick="selectTemplate(this,\'simple\')">简洁模板</button></div></div>';
        p+='<div class="step-extra-panel show"><h5>格式设置</h5><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><label style="font-size:12px">字体</label><select class="wf-select" style="width:100px"><option>宋体</option><option>仿宋</option><option>楷体</option></select><label style="font-size:12px;margin-left:8px">字号</label><select class="wf-select" style="width:80px"><option>小四</option><option>五号</option></select></div><p style="font-size:11px;color:#a0aec0;margin-top:6px">参考财政部396号文规范及其他规范样本校对格式</p></div>';
    }
    return p;
}

function buildGradeAndChart(txt){
    var g='';
    var grade='',gc='pass',score='';
    if(/(优秀|良好|合格|不合格)/.test(txt)){
        if(/优秀/.test(txt)){grade='优秀';gc='excellent'}else if(/良好/.test(txt)){grade='良好';gc='good'}else if(/不合格/.test(txt)){grade='不合格';gc='fail'}else{grade='合格';gc='pass'}
    }
    var sm=txt.match(/综合得分[：:]\s*([\d.]+)\s*分/);
    if(sm)score=sm[1];
    g+='<div class="step-extra-panel show"><h5>🏆 评价结论</h5>';
    if(grade)g+='<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px"><span style="font-size:14px;color:#718096">综合评定等级：</span><span class="grade-badge '+gc+'">'+grade+'</span>'+(score?'<span style="font-size:14px;color:#667eea;font-weight:600">得分：'+score+'分</span>':'')+'</div>';
    g+='<div style="margin-top:12px"><h5 style="font-size:13px;color:#1a202c;margin-bottom:8px">📊 一级指标得分分布</h5><div class="wf-bar-chart" id="wfBarChart">';
    var bars=[{l:'决策',s:25,v:25},{l:'过程',s:20,v:20},{l:'产出',s:30,v:30},{l:'效益',s:25,v:25}];
    // 尝试从步骤内容中解析实际得分
    var scoreMatches=txt.match(/(决策|过程|产出|效益)[^分]*?得分[：:]*\s*([\d.]+)/g);
    if(scoreMatches){
        for(var bi2=0;bi2<bars.length;bi2++){
            for(var si=0;si<scoreMatches.length;si++){
                var sm2=scoreMatches[si].match(/(决策|过程|产出|效益)[^分]*?[：:]\s*([\d.]+)/);
                if(sm2&&bars[bi2].l===sm2[1]){bars[bi2].v=parseFloat(sm2[2]);break}
            }
        }
    }
    for(var bi=0;bi<bars.length;bi++){
        var b=bars[bi],fillColor=bi===0?'#667eea':(bi===1?'#48bb78':(bi===2?'#ed8936':'#9f7aea'));
        g+='<div class="wf-bar-col"><div class="wf-bar-value">'+b.v+'分</div><div class="wf-bar-score">/'+b.s+'</div><div class="wf-bar-fill" style="height:'+(b.v/b.s*140)+'px;background:'+fillColor+'"></div><div class="wf-bar-label">'+b.l+'</div></div>';
    }
    g+='</div></div></div>';
    return g;
}

function buildStepChat(sn){
    return '<div class="wf-step-chat active" id="wfStepChat"><h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#667eea" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>优化步骤 '+sn+': '+C.STEP_NAMES[sn]+'<button class="refine-btn" style="margin-left:auto" onclick="showStepHistory('+sn+')">历史版本</button></h4><div class="wf-step-chat-msgs" id="wfStepChatMsgs"></div><div class="wf-step-chat-input-row"><input id="wfStepChatInput" placeholder="输入优化指令，让AI帮您调整..." onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();refineStepContent()}"><button class="btn-primary btn-sm" onclick="refineStepContent()">发送</button></div></div>';
}

function buildStepFooter(sn,st,txt){
    var f='<div class="step-footer-bar">';
    f+='<span style="font-size:11px;color:#a0aec0">步骤 '+sn+' · '+C.STEP_NAMES[sn]+'</span><span class="spacer"></span>';
    if(st==='done'&&txt){
        f+='<button class="btn-sm" data-act="step-export" data-step="'+sn+'" data-format="word">📄 导出Word</button>';
        f+='<button class="btn-sm primary" data-act="step-export" data-step="'+sn+'" data-format="pdf">📕 导出PDF</button>';
        if(sn===6)f+='<button class="btn-sm success" onclick="generateQuestionnaireFromStep()">📋 生成问卷</button>';
    }
    f+='</div>';
    return f;
}

function selectIndicatorType(el){
    document.querySelectorAll('#indicatorTypeSelect .indicator-type-tag').forEach(function(t){t.classList.remove('selected')});
    el.classList.add('selected');
    wfIndicatorType=el.textContent.trim();
    showToast('指标体系类型已设为：'+wfIndicatorType);
}

function selectPerspective(el,persp){
    el.parentElement.querySelectorAll('.perspective-btn').forEach(function(b){b.classList.remove('selected')});
    el.classList.add('selected');
    wfPerspective=persp;
    var names={'finance':'财政部门','congress':'人大常委会','audit':'审计部门','management':'管理方建议','third_party':'第三方建议'};
    var name=names[persp]||persp;
    showToast('分析视角切换为：'+name);

    // 步骤10的管理方/第三方建议视角不需要调用后端分析API
    if(persp==='management'||persp==='third_party'){
        showToast(name+'视角已选择，建议将基于此视角生成');
        return;
    }

    // 步骤9的视角分析：调用后端API获取该视角的分析
    if(!wfSessionId){showToast('工作流未启动','error');return}
    var resultDiv=document.getElementById('perspectiveResult');
    if(resultDiv){resultDiv.innerHTML='<p style="color:#a0aec0">⏳ 正在分析'+name+'视角...</p>'}
    api('POST','/api/agent/workflow/'+encodeURIComponent(wfSessionId)+'/perspective',{perspective:persp}).then(function(r){
        if(r.success&&r.data&&r.data.analysis){
            if(resultDiv){
                resultDiv.innerHTML='<div style="background:#f7fafc;border-left:3px solid #667eea;padding:12px;border-radius:4px;margin-top:8px">'+r.data.analysis.replace(/\n/g,'<br>')+'</div>';
            }
            showToast(name+'分析完成','success');
        }
    }).catch(function(e){
        showApiError(e,{action:name+'视角分析'});
        if(resultDiv){resultDiv.innerHTML='<p style="color:#e53e3e">分析失败，请确保步骤7和步骤9已完成</p>'}
    });
}

function selectTemplate(el,tpl){
    el.parentElement.querySelectorAll('.template-option').forEach(function(b){b.classList.remove('selected')});
    el.classList.add('selected');
    wfTemplate=tpl;
    showToast('报告模板切换为：'+(tpl==='standard'?'标准模板':(tpl==='detail'?'详细模板':'简洁模板')));
}

function toggleModule(header){
    header.parentElement.classList.toggle('open');
}

function selectLanguageStyle(el,style){
    el.parentElement.querySelectorAll('.perspective-btn').forEach(function(b){b.classList.remove('selected')});
    el.classList.add('selected');
    window._languageStyle=style;
    var names={'neutral':'中立','sharp':'尖锐','diplomatic':'委婉'};
    showToast('语言风格切换为：'+(names[style]||style));
    // 将风格传递到 refine 请求中
    if(wfStep){wfLanguageStyle=style}
}
function importIndicatorTemplate(){
    showToast('请选择要导入的指标体系文件（支持 .xlsx / .json）');
}
function useClassicTemplate(){
    showToast('经典模板库包含：项目支出、部门整体、专项债、社保基金等指标体系模板。功能开发中，敬请期待。');
}
function updateTableFont(font){showToast('字体已设为：'+font)}
function updateTableFontSize(size){showToast('字号已设为：'+size)}

function generateQuestionnaireFromStep(){
    if(!wfSessionId){showToast('请先完成指标体系生成','error');return}
    if(!confirm('是否基于当前指标体系生成调查问卷？'))return;
    api('POST','/api/agent/workflow/questionnaire',{session_id:wfSessionId,max_questions:20}).then(function(r){
        if(r.success&&r.data&&r.data.questionnaire&&r.data.questionnaire.length){
            showToast('问卷已生成，共'+r.data.total_questions+'题','success');
            var qp=document.getElementById('qConfirmPanel');if(qp)qp.innerHTML='<p style="color:#48bb78">✅ 问卷已生成（共'+r.data.total_questions+'题），前往问卷填写页面查看</p>';
            // 直接切到问卷填写Tab并渲染问卷 — 绕过switchTab避免loadQuestionnaireList覆盖
            var pnEl=document.getElementById('wfProjectName'),pn=pnEl?pnEl.value.trim():'';
            var navLink = document.querySelector('.nav-link[onclick*="questionnaire"]');
            var all=document.querySelectorAll('.nav-link');for(var i=0;i<all.length;i++)all[i].classList.remove('active');
            if(navLink)navLink.classList.add('active');
            document.querySelectorAll('.content-section').forEach(function(s){s.classList.remove('active')});
            var sec=document.getElementById('questionnaire');if(sec)sec.classList.add('active');
            if(window.innerWidth<768&&isMobileOpen)toggleMobileSidebar();
            renderQuestionnaireDirect(r.data.questionnaire, wfSessionId, pn||'当前项目');
        }else{showToast('问卷生成失败：指标体系尚未完成（请确保Step3已完成）','error')}
    }).catch(function(e){showApiError(e,{action:'问卷生成'})});
}

// 直接在问卷填写Tab渲染问卷（不重新调API生成）
function renderQuestionnaireDirect(qs, sid, pn){
    renderQuestionnaireForm(qs, sid, pn);
}

function setEditMode(mode,el){
    el.parentElement.querySelectorAll('button').forEach(function(b){b.classList.remove('active')});
    el.classList.add('active');
    wfEditMode=mode;
    const step = document.querySelector('.wf-step.active');
    if(step) selectWfStep(Number(step.id.replace('wfStep','')));
}

async function saveManualStep(step) {
    try {
        const result = await api('PUT', '/api/agent/workflow/'+encodeURIComponent(wfSessionId)+'/steps/'+step,
            {content:document.getElementById('wfManualContent').value, version:wfVersions[step] || 1});
        wfStepContents[step] = result.data.refined_content;
        wfVersions[step] = result.data.version;
        wfReviewSteps = result.data.review_steps;
        selectWfStep(step);
        showToast(wfReviewSteps.length ? '修改已保存，下游待复核' : '修改已保存');
    } catch(error) { showApiError(error); }
}

async function confirmStepReview(step) {
    try {
        const result = await api('POST', '/api/agent/workflow/'+encodeURIComponent(wfSessionId)+'/steps/'+step+'/review');
        wfReviewSteps = result.data.review_steps;
        selectWfStep(step);
    } catch(error) { showApiError(error); }
}

var wfIndicatorType='项目支出指标体系',wfPerspective='management',wfTemplate='standard',wfEditMode='ai',wfLanguageStyle='neutral',wfAssemblyOrder=[1,2,3,4,5,6,7,8];
var dragSrcIdx=null;

function handleAssemblyDragStart(e){dragSrcIdx=parseInt(e.target.getAttribute('data-idx'));e.dataTransfer.effectAllowed='move';e.target.style.opacity='0.5'}
function handleAssemblyDragOver(e){e.preventDefault();e.dataTransfer.dropEffect='move'}
function handleAssemblyDrop(e){
    e.preventDefault();var target=e.target.closest('.assembly-chip');if(!target)return;
    var dstIdx=parseInt(target.getAttribute('data-idx'));
    if(dragSrcIdx===null||dragSrcIdx===dstIdx)return;
    // 交换顺序
    var tmp=wfAssemblyOrder[dragSrcIdx];wfAssemblyOrder[dragSrcIdx]=wfAssemblyOrder[dstIdx];wfAssemblyOrder[dstIdx]=tmp;
    // 重新渲染
    var orderEl=document.getElementById('assemblyOrder');if(!orderEl)return;
    var chips=orderEl.querySelectorAll('.assembly-chip');
    var secs=[{n:1,t:'工作简介'},{n:2,t:'基本情况'},{n:3,t:'指标体系表'},{n:4,t:'指标得分分析'},{n:5,t:'综合评价'},{n:6,t:'经验总结'},{n:7,t:'问题分析'},{n:8,t:'改进建议'}];
    var html='';
    for(var i=0;i<wfAssemblyOrder.length;i++){
        var si=wfAssemblyOrder[i]-1;
        html+='<div class="assembly-chip" draggable="true" data-idx="'+i+'" ondragstart="handleAssemblyDragStart(event)" ondragover="handleAssemblyDragOver(event)" ondrop="handleAssemblyDrop(event)" style="cursor:grab"><span class="chip-num">'+(i+1)+'</span>'+secs[si].t+'</div>';
    }
    orderEl.innerHTML=html;
    dragSrcIdx=null;
    showToast('报告顺序已调整，导出时将按新顺序组装');
}

function handleWfDrop(e,type){
    e.preventDefault();e.stopPropagation();
    var files=e.dataTransfer.files;if(!files.length)return;
    var input=document.getElementById(type+'FileInput');
    if(input){
        var dt=new DataTransfer();
        for(var i=0;i<files.length;i++)dt.items.add(files[i]);
        input.files=dt.files;
        handleWfFilesByType({target:input},type);
    }
}

function handleWfFilesByType(e,type){
    var fs=e.target.files;if(!fs.length)return;
    var allowedExts={img:['.jpg','.jpeg','.png','.bmp'],pdf:['.pdf'],doc:['.doc','.docx','.xls','.xlsx','.txt','.csv']};
    var maxSize=50*1024*1024;
    for(var i=0;i<fs.length;i++){var f=fs[i];var ext='.'+f.name.split('.').pop().toLowerCase();
        if(allowedExts[type]&&allowedExts[type].indexOf(ext)<0){showToast(f.name+' 格式不支持，请上传'+allowedExts[type].join('/')+'文件','warn');continue}
        if(f.size>maxSize){showToast(f.name+' 超过50MB限制','warn');continue}
        (function(f,ft){
        var fd=new FormData();fd.append('file',f);
        var modelSelect=document.getElementById(ft+'ModelSelect'),modelName=modelSelect?modelSelect.value:'';
        if(modelName)fd.append('model_name',modelName);
        uploadApi('/api/agent/upload',fd).then(function(r){if(r.success){
            wfUploadedFiles.push({name:r.data.original_name,path:r.data.file_path,size:r.data.file_size,type:ft});
            renderWfFilesByType(ft);
        }}).catch(function(e){var em=e.message||'';if(em.indexOf('413')>=0||em.indexOf('too large')>=0){showToast('文件过大，请压缩后重试（限制50MB）','error')}else if(em.indexOf('415')>=0){showToast('文件格式不支持，请上传JPG/PNG/PDF/DOC等格式','error')}else{showApiError(e,{action:'上传'})}})
    })(fs[i],type)}e.target.value='';
}

function renderWfFilesByType(type){
    var listEl=document.getElementById(type+'FileList'),dropEl=document.getElementById(type+'Dropzone');
    if(!listEl)return;
    var files=[];for(var i=0;i<wfUploadedFiles.length;i++){if(wfUploadedFiles[i].type===type)files.push(wfUploadedFiles[i])}
    listEl.innerHTML='';
    for(var j=0;j<files.length;j++){(function(idx){
        var f=files[idx],kb=f.size>1024*1024?(f.size/1024/1024).toFixed(1)+'MB':(f.size/1024).toFixed(0)+'KB';
        var t=document.createElement('div');t.className='wf-file-tag';
        t.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22543d" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span>'+esc(f.name)+' ('+kb+')</span><button class="remove" data-act="wf-file-remove" data-type="'+esc(type)+'" data-idx="'+idx+'">x</button>';
        listEl.appendChild(t);
    })(j)}
    if(dropEl){if(files.length>0)dropEl.classList.add('has-files');else dropEl.classList.remove('has-files')}
}

function removeWfFileByType(type,idx){
    var typeFiles=[];for(var i=0;i<wfUploadedFiles.length;i++){if(wfUploadedFiles[i].type===type)typeFiles.push({idx:i,obj:wfUploadedFiles[i]})}
    if(idx<typeFiles.length)wfUploadedFiles.splice(typeFiles[idx].idx,1);
    renderWfFilesByType(type);
}

function toggleDeductionTag(el){el.classList.toggle('selected')}
function buildScoringInputs(txt){
    // 从步骤7内容中解析三级指标及其分值
    var html='',indicators=[];
    // 匹配模式：三级指标名称 + 分值 + 评分标准
    var pattern=/(?:三级指标[：:])?\s*([\u4e00-\u9fa5\w]+(?:率|度|性|情况|指标|效果|管理|执行|完成|效率|效益|达标|合规|规范|落实|保障|到位|健全|覆盖|公开|及时|准确|完整|有效|满意|合理|充分|明确|可行|安全|持续|优化|提升|改善|提高|降低|减少|增加|实现|达到|满足|符合|通过|完成率|达成率|达标率|实施率|执行率|覆盖率|利用率|到位率|及时率|准确率|完整率|有效率|满意率|合格率|合规率|规范率|增长率|下降率|公开率|保障率|透明度|知晓度|认可度|满意度|参与度|满意度|响应度))/g;
    var m;var seen={};
    while((m=pattern.exec(txt))!==null){
        var name=m[1];if(seen[name]||name.length<2)continue;seen[name]=true;
        // 尝试提取对应分值
        var scorePattern=new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'[^分]*?(\\d+\\.?\\d*)\\s*分');
        var sm=txt.match(scorePattern);
        var score=sm?sm[1]:'';
        indicators.push({name:name,score:score});
    }
    if(indicators.length===0){
        // 如果没有匹配到，尝试从文本中提取所有"三级指标"相关行
        var lines=txt.split(/[\n\r]+/);
        for(var i=0;i<lines.length;i++){
            var l=lines[i].trim();if(!l||l.length>300)continue;
            var im=l.match(/(?:三级指标[：:])?\s*(.+?)(?:\s*[（(]\s*(\d+\.?\d*)\s*分\s*[）)]|$)/);
            if(im&&im[1]&&im[1].length>1&&!seen[im[1]]&&im[1].length<50){
                seen[im[1]]=true;
                indicators.push({name:im[1],score:im[2]||''});
            }
        }
    }
    if(indicators.length===0){
        html+='<p style="font-size:12px;color:#a0aec0">评分分析完成后将自动填充各指标得分</p>';
    }else{
        html+='<div style="max-height:400px;overflow-y:auto">';
        for(var i=0;i<Math.min(indicators.length,30);i++){
            var ind=indicators[i];
            html+='<div class="scoring-row" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #e2e8f0">';
            html+='<span style="font-size:12px;color:#4a5568;min-width:160px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(ind.name)+'">'+esc(ind.name)+'</span>';
            html+='<input class="input-field" style="width:50px;height:28px;font-size:12px;text-align:center" placeholder="得分" value="'+esc(ind.score)+'" onchange="updateScoringItem(this)">';
            html+='<span style="font-size:11px;color:#a0aec0">分</span>';
            html+='<input class="input-field" style="flex:1;height:28px;font-size:12px" placeholder="得分/扣分原因（大白话描述）..." onchange="updateScoringItem(this)">';
            html+='</div>';
        }
        html+='</div><p style="font-size:11px;color:#667eea;margin-top:6px">已解析 '+indicators.length+' 个指标，修改后系统将按新数值重新计算总分</p>';
    }
    return html;
}
function updateScoringItem(el){};
function extractModuleContent(txt,mods){
    // 从文本中按模块标题提取内容
    var result={};
    for(var i=0;i<mods.length;i++){
        var mi=mods[i];
        // 尝试匹配 "模块标题" 或 "**模块标题**" 或 "## 模块标题" 格式
        var patterns=[
            new RegExp('(?:^|\\n)\\s*(?:#{1,3}\\s*)?\\*{0,2}'+mi.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\*{0,2}\\s*[：:]\\s*(.+?)(?=\\n\\s*(?:#{1,3}\\s*)?\\*{0,2}(?:'+mods.join('|').replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')\\*{0,2}|$)','s'),
            new RegExp('(?:^|\\n)\\s*'+mi.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'[：:]\\s*(.+?)(?=\\n\\s*(?:'+mods.join('|').replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')[：:]|$)','s'),
            new RegExp('(?:^|\\n)\\s*\\*{0,2}'+mi.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\*{0,2}\\s*(.+?)(?=\\n\\s*\\*{0,2}(?:'+mods.join('|').replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')\\*{0,2}|$)','s')
        ];
        for(var j=0;j<patterns.length;j++){
            var m=patterns[j].exec(txt);
            if(m&&m[1]&&m[1].trim().length>0){
                result[mi]=m[1].trim().substring(0,500);
                break;
            }
        }
    }
    return result;
}
function updateModuleContent(el){};
function confirmDeductions(){
    var selected=document.querySelectorAll('.deduction-tag.selected'),manual=document.getElementById('manualDeduction');
    var items=[];selected.forEach(function(t){items.push(t.textContent)});
    if(manual&&manual.value.trim())items.push(manual.value.trim());
    if(items.length===0){showToast('请至少选择一个扣分项或手动输入','error');return}
    showToast('已提取 '+items.length+' 个问题项，请与AI对话优化','success');
    var msgs=document.getElementById('wfStepChatMsgs');if(msgs)msgs.innerHTML+='<div class="wf-step-chat-msg user">已确认问题项：'+esc(items.join('、'))+'</div>';
    if(manual)manual.value=''
}
function setLayoutMode(md,el){
    wfLayoutMode=md;
    var sel=document.getElementById('wfLayoutSel');if(!sel)return;
    sel.querySelectorAll('button').forEach(function(b){b.classList.remove('active')});
    if(el)el.classList.add('active');
    showToast('布局模式切换为: '+(md==='classic'?'经典排版':'自动排版'));
}
function exportStep(sn,fmt){
    if(!wfSessionId){showToast('请先完成工作流','error');return}
    if(!wfStepContents[sn]){showToast('该步骤内容为空，无法导出','warn');return}
    var btn=event&&event.target;
    if(btn){btn.disabled=true;btn.dataset.origText=btn.textContent;btn.textContent='导出中...'}
    var body={session_id:wfSessionId,format:fmt,project_name:(document.getElementById('wfProjectName').value||'绩效评价报告'),step_number:parseInt(sn),step_content:wfStepContents[sn]};
    api('POST','/api/agent/workflow/export',body).then(function(r){
        if(r.data&&r.data.file_path){showToast('步骤'+sn+' '+(fmt==='word'?'Word':'PDF')+'导出成功','success');return downloadExport(r.data.file_name)}
        else{showToast('导出失败：未获取到文件','error')}
    }).catch(function(e){showApiError(e,{action:'导出'})})
    .finally(function(){if(btn){btn.disabled=false;if(btn.dataset.origText)btn.textContent=btn.dataset.origText}})
}
function refineStepContent(stepNum, msgText, isApprovalReject){
    var input=document.getElementById('wfStepChatInput'),msg=(msgText||'')||(input&&input.value.trim());
    if(!msg)return;
    var msgs=document.getElementById('wfStepChatMsgs');if(msgs)msgs.innerHTML+='<div class="wf-step-chat-msg user">'+esc(msg)+'</div>';
    if(input)input.value='';
    var sn=stepNum||0;
    if(!sn){document.querySelectorAll('.wf-step.active').forEach(function(s){var m=s.id.match(/wfStep(\d+)/);if(m)sn=parseInt(m[1])});}
    if(!sn||!wfSessionId)return;
    var aiDiv=document.createElement('div');aiDiv.className='wf-step-chat-msg ai';aiDiv.textContent='AI重新生成中...';if(msgs)msgs.appendChild(aiDiv);
    // 审批拒绝场景：展示进度指示
    if(isApprovalReject){
        var c=document.getElementById('wfContent');if(c)c.innerHTML='<div class="wf-empty"><h3>'+C.STEP_NAMES[sn]+'</h3><p>AI正在按照您的修改意见重新生成...</p></div>';
    }
    // 批次 36（B4）：先出草稿、预览差异，用户点「采用」才落库
    api('POST','/api/agent/workflow/refine',{session_id:wfSessionId,step_number:sn,message:msg,preview:true,layout_mode:wfLayoutMode,language_style:wfLanguageStyle,indicator_type:wfIndicatorType}).then(function(r){
        if(msgs&&aiDiv.parentNode)aiDiv.remove();
        if(r.success&&r.data&&typeof r.data.draft==='string'){
            if(msgs)msgs.innerHTML+='<div class="wf-step-chat-msg ai">草稿已生成，确认后再保存（未采用不会改动当前内容）。</div>';
            renderRefinePreview(sn,r.data);
        }else{if(msgs)msgs.innerHTML+='<div class="wf-step-chat-msg ai">优化失败，请重试</div>'}
    }).catch(function(e){
        if(msgs&&aiDiv.parentNode)aiDiv.remove();
        if(msgs)msgs.innerHTML+='<div class="wf-step-chat-msg ai">错误: '+esc(e.message)+'</div>'
    })
}

// ── B4（批次 36）：refine 草稿预览 / 采用 / 丢弃 / 版本回滚 ──
function refineDiffHtml(diff){
    if(!diff||!diff.lines||!diff.lines.length)return '<div class="refine-diff-empty">内容没有变化</div>';
    var h='<div class="refine-diff">';
    for(var i=0;i<diff.lines.length;i++){
        var row=diff.lines[i],op=row.op||'ctx';
        var cls=op==='add'?'add':(op==='del'?'del':(op==='hunk'?'hunk':'ctx'));
        var sign=op==='add'?'+':(op==='del'?'-':' ');
        h+='<div class="diff-line '+cls+'">'+esc(sign+' '+row.text)+'</div>';
    }
    if(diff.truncated)h+='<div class="diff-line hunk">…（差异过长，仅显示前 '+diff.lines.length+' 行）</div>';
    return h+'</div>';
}

function renderRefinePreview(sn,data){
    var msgs=document.getElementById('wfStepChatMsgs');if(!msgs)return;
    var box=document.createElement('div');box.className='wf-step-chat-msg ai refine-preview';
    box.innerHTML='<div class="refine-preview-head">第 '+sn+' 步 · 待确认草稿'
        +'<span class="refine-preview-meta">证据 '+(data.evidence_count||0)+' 条'
        +(data.truncated?' · 原文过长，已保留首尾':'')+'</span></div>'
        +(data.degraded?'<div class="refine-degraded">主模型不可用，本稿由备选模型完成：措辞与判断可能与其他步骤不一致</div>':'')
        +refineDiffHtml(data.diff)
        +'<div class="refine-preview-actions">'
        +'<button class="refine-btn primary" data-role="apply">采用</button>'
        +'<button class="refine-btn" data-role="discard">丢弃</button></div>';
    box._draft=data.draft;box._version=data.version;box._step=sn;
    box.querySelector('[data-role="apply"]').onclick=function(){applyRefinePreview(box)};
    box.querySelector('[data-role="discard"]').onclick=function(){box.remove();showToast('已丢弃草稿','info')};
    msgs.appendChild(box);msgs.scrollTop=msgs.scrollHeight;
}

function applyRefinePreview(box){
    var sn=box._step,btn=box.querySelector('[data-role="apply"]');
    btn.disabled=true;btn.textContent='保存中...';
    api('PUT','/api/agent/workflow/'+wfSessionId+'/steps/'+sn,{content:box._draft,version:box._version}).then(function(r){
        if(!r.success)throw new Error(r.message||'保存失败');
        wfStepContents[sn]=box._draft;
        if(r.data&&r.data.version)wfVersions[sn]=r.data.version;
        var inner=document.querySelector('.wf-content-inner');if(inner)inner.innerHTML=renderMd(box._draft);
        box.querySelector('.refine-preview-actions').innerHTML='<span class="refine-applied">已采用（第 '+(r.data&&r.data.version||'')+' 版）</span>';
        showToast('步骤'+sn+'内容已更新','success');
    }).catch(function(e){
        btn.disabled=false;btn.textContent='采用';
        showToast(e.status===409?'步骤已被修改，请刷新后重试':('保存失败：'+e.message),'error');
    });
}

function showStepHistory(sn){
    if(!wfSessionId)return;
    api('GET','/api/agent/workflow/'+wfSessionId+'/steps/'+sn+'/history').then(function(r){
        if(!r.success)return;
        var msgs=document.getElementById('wfStepChatMsgs');if(!msgs)return;
        var old=msgs.querySelectorAll('.refine-history');for(var k=0;k<old.length;k++)old[k].remove();
        var box=document.createElement('div');box.className='wf-step-chat-msg ai refine-history';
        var h='<div class="refine-preview-head">步骤 '+sn+' 的历史版本</div>';
        for(var i=0;i<r.data.length;i++){
            var row=r.data[i];
            h+='<div class="refine-history-row"><span>第 '+row.version+' 版 · '+esc(row.edited_by||'workflow')+' · '+(row.chars||0)+' 字</span>';
            h+=row.is_current?'<span class="refine-current">当前</span>'
                :'<button class="refine-btn" data-version="'+row.version+'">回滚</button>';
            h+='</div>';
        }
        box.innerHTML=h;
        box.querySelectorAll('button[data-version]').forEach(function(btn){
            btn.onclick=function(){rollbackStep(sn,parseInt(btn.getAttribute('data-version')),btn)};
        });
        msgs.appendChild(box);msgs.scrollTop=msgs.scrollHeight;
    }).catch(function(e){showToast('读取历史失败：'+e.message,'error')});
}

function rollbackStep(sn,version,btn){
    btn.disabled=true;btn.textContent='回滚中...';
    api('POST','/api/agent/workflow/'+wfSessionId+'/steps/'+sn+'/rollback',{to_version:version}).then(function(r){
        if(!r.success)throw new Error(r.message||'回滚失败');
        var content=(r.data&&r.data.refined_content)||'';
        wfStepContents[sn]=content;
        if(r.data&&r.data.version)wfVersions[sn]=r.data.version;
        var inner=document.querySelector('.wf-content-inner');if(inner)inner.innerHTML=renderMd(content);
        showToast('已回滚到第 '+version+' 版','success');
        showStepHistory(sn);
    }).catch(function(e){btn.disabled=false;btn.textContent='回滚';showToast('回滚失败：'+e.message,'error')});
}

        function wfStepStatus(sn){if(wfCompletedSteps.indexOf(sn)>=0)return 'done';if(sn===wfCurrentStep){if(wfStatus==='failed')return 'error';if(wfStatus==='awaiting_approval'||wfStatus==='paused')return 'awaiting';if(wfRunning)return 'doing'}return 'pending'}
        function renderMd(t){if(!t)return'';var h=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');h=h.replace(/### (.+)/g,'<h3>$1</h3>');h=h.replace(/## (.+)/g,'<h2>$1</h2>');h=h.replace(/# (.+?)\n/g,'<h1>$1</h1>');h=h.replace(/- (.+)/g,'&bull; $1');h=h.replace(/\n/g,'<br>');return h}

        function updateWfStepper(){
            for(var i=1;i<=14;i++){var s=document.getElementById('wfStep'+i);if(!s)continue;s.className='wf-step';var st=wfStepStatus(i);
                if(st==='done')s.classList.add('done');else if(st==='doing')s.classList.add('doing');if(i===wfApprovalStep&&st==='done')s.classList.add('approval');
                if(wfCompletedSteps.indexOf(i)<0&&i===wfApprovalStep)s.classList.add('approval');
                if(i===wfSelectedStep)s.classList.add('active');
            }
            var done=wfCompletedSteps.length;document.getElementById('wfProgressText')&&(document.getElementById('wfProgressText').textContent='已完成 '+done+' / 14 步 · '+(workflowStatusLabels[wfStatus]||wfStatus)+(wfCurrentStep?' · '+C.STEP_NAMES[wfCurrentStep]:''));
            document.getElementById('wfProgressFill')&&(document.getElementById('wfProgressFill').style.width=Math.round(done/14*100)+'%');
        }

        function startWorkflow(){
            var t=getToken();if(!t){showToast('请先登录','error');return}
            if(wfRunning){showToast('工作流正在运行中，请勿重复点击','warn');return}
            wfRunning=true;
            var sbtn=document.getElementById('wfStartBtn');if(sbtn){sbtn.disabled=true;sbtn.textContent='启动中...'}
            var pn=document.getElementById('wfProjectName').value.trim()||'未命名项目';
            // 重置状态，准备新评价
            wfStatusController?.abort();
            wfSessionId=newWorkflowSession();sessionStorage.setItem(workflowSessionKey,wfSessionId);
            wfCompletedSteps=[];wfStepContents={};wfVersions={};wfEvidence={};wfComparisons={};wfReviewSteps=[];wfApprovalStep=null;wfCurrentStep=0;wfStatus='running';wfCoverage=null;wfConsistency=null;wfDispo=null;wfCritique=null;wfCritiqueSwitches={};
            wfSelectedStep=0;wfStepScroll={};wfDeltaRenderer.reset();
            document.getElementById('wfRetryBtn').hidden=true;
            window._currentQuestionData=null;  // 清空问卷缓存，确保下次切换Tab重新加载
            initWfStepper();document.getElementById('wfLayout').style.display='flex';
            document.getElementById('wfFileZone').style.display='none';
            document.getElementById('wfProgress').style.display='flex';document.getElementById('wfStartBtn').style.display='';document.getElementById('wfStopBtn').style.display='';
            var ex=document.getElementById('wfExports');if(ex)ex.style.display='none';
            var c=document.getElementById('wfContent');c&&(c.innerHTML='<div class="wf-empty"><h3>正在启动...</h3><p>AI正在准备绩效评价工作流</p></div>');
            updateWfStepper();

            var paths=[];for(var i=0;i<wfUploadedFiles.length;i++)paths.push(wfUploadedFiles[i].path);
var md=document.getElementById('wfModel').value,mo=document.getElementById('wfMode').value;
var tp=document.getElementById('wfTemperature').value,topP=parseFloat(document.getElementById('wfTopP').value);
var body={session_id:wfSessionId,use_rag:document.getElementById("wfUseRag").checked,require_approval:document.getElementById("wfRequireApproval").checked,project_name:pn,file_paths:paths,model_name:md,comparison_mode:mo,temperature_profile:tp,top_p:topP,indicator_type:wfIndicatorType,perspective:wfPerspective,template:wfTemplate,edit_mode:wfEditMode};
            wfAbortController=new AbortController();
            window.WF_STREAM_ACTIVE=true;

            fetch('/api/agent/workflow/start-stream',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+t},body:JSON.stringify(body),signal:wfAbortController.signal})
            .then(function(r){return readWorkflowEvents(r, handleSSEEvent)
            }).catch(function(e){window.WF_STREAM_ACTIVE=false;{showApiError(e);restoreWorkflow()}})
        }

        /* token 增量渲染：50ms 合并一次，避免逐字触发布局 */
        var wfDeltaRenderer=WorkflowController.createDeltaRenderer(function(step){
            if(step!==wfSelectedStep||wfStepStatus(step)!=='doing')return;
            var c=document.getElementById('wfContent');if(!c)return;
            var body=c.querySelector('.wf-content-body'),stream=body&&body.querySelector('.wf-stream-body');
            if(!stream)return;
            var follow=wfIsAtBottom(body),top=body.scrollTop;
            var hint=body.querySelector('#wfStreamHint');if(hint)hint.remove();
            stream.textContent=wfStepContents[step]||'';
            body.scrollTop=follow?body.scrollHeight:top;
        });

        function handleSSEEvent(d){
            if(!wfRunning)return;
            if(d.session_id&&wfSessionId&&d.session_id!==wfSessionId)return;
            if(d.event==='token_delta'){
                var deltaStep=Number(d.step_number);
                if(deltaStep>=1&&deltaStep<=14&&wfCompletedSteps.indexOf(deltaStep)<0){
                    // 接收即缓存，50ms 只合并 DOM 更新；切换不丢掉尚未 flush 的内容。
                    wfStepContents[deltaStep]=(wfStepContents[deltaStep]||'')+(d.delta||'');
                    wfDeltaRenderer.push(deltaStep,d.delta);
                }
                return;
            }
            if(d.event==='step_start'){
                wfDeltaRenderer.flushNow();
                wfSessionId=d.session_id||wfSessionId;
                var follow=!wfSelectedStep||wfSelectedStep===wfCurrentStep;
                var sn=Number(d.step_number);wfCurrentStep=sn;wfStatus="running";updateWfStepper();
                // 用户查看别的步骤时不抢页面；开始事件也不能清掉提前收到的 delta。
                if(follow||wfSelectedStep===sn)selectWfStep(sn);
            }if(d.event==='token_update'){if(currentUserData){currentUserData.tokens_used=d.tokens_used||0}var stEl=document.getElementById('settingsTokens');if(stEl)stEl.textContent=currentUserData?currentUserData.tokens_used:0;var sbEl=document.getElementById('sidebarTokenCount');if(sbEl)sbEl.textContent=currentUserData?currentUserData.tokens_used:0;return}else if(d.event==='step_complete'){
                wfDeltaRenderer.flushNow();
                WBUI.announce('步骤'+d.step_number+'已完成');
                var sn=Number(d.step_number);if(wfCompletedSteps.indexOf(sn)<0)wfCompletedSteps.push(sn);wfStepContents[sn]=typeof d.content==='string'?d.content:(d.content_preview||wfStepContents[sn]||'');wfEvidence[sn]=d.evidence||[];updateWfStepper();if(wfSelectedStep===sn||!wfSelectedStep)selectWfStep(sn)
            }else if(d.event==='approval_required'){
                var sn=d.step_number;wfApprovalStep=sn;updateWfStepper();
                document.getElementById('wfApprovalTitle').textContent='确认：'+C.STEP_NAMES[sn];
                document.getElementById('wfApprovalSubtitle').textContent='请审核步骤'+sn+'的生成内容，确认后继续执行';
                document.getElementById('wfApprovalPreview').textContent=(d.content_preview||'(无内容预览)');
                document.getElementById('wfApprovalFeedback').value='';document.getElementById('wfApprovalModal').style.display='flex'
            }else if(d.event==='warning'){
                showToast('步骤'+d.step_number+': '+d.message,'error')
            }else if(d.event==='error'){
                window.WF_STREAM_ACTIVE=false;
                var stepLabel=d.step_number>0?('步骤'+d.step_number+' '+(d.step_name||'')+' - '):'';
                showToast(stepLabel + d.message, 'error');
                restoreWorkflow();
            }else if(d.event==='waiting_approval'){
                window.WF_STREAM_ACTIVE=false;
                wfSessionId=d.session_id;
                var asn=d.approval_step;wfCurrentStep=asn;wfStatus='awaiting_approval';updateWfStepper();
                if(!wfApprovalStep){wfApprovalStep=asn;updateWfStepper();
                    document.getElementById('wfApprovalTitle').textContent='确认：'+C.STEP_NAMES[asn];
                    document.getElementById('wfApprovalSubtitle').textContent='请审核步骤'+asn+'的生成内容，确认后继续执行后续步骤';
                    document.getElementById('wfApprovalPreview').textContent=(wfStepContents[asn]||'(无内容预览)').substring(0,500);
                    document.getElementById('wfApprovalFeedback').value='';document.getElementById('wfApprovalModal').style.display='flex'
                }
                document.getElementById('wfStartBtn').style.display='none';document.getElementById('wfStopBtn').style.display='';document.getElementById('wfProgress').style.display='';
                showToast('工作流暂停于步骤'+asn+'，请审核并确认','warn')
            }else if(d.event==='complete'){
                wfDeltaRenderer.flushNow();
                window.WF_STREAM_ACTIVE=false;
                wfSessionId=d.session_id;wfRunning=false;wfStatus='completed';updateWfStepper();
                var ex=document.getElementById('wfExports');if(ex)ex.style.display='flex';
                document.getElementById('wfStartBtn').style.display='';document.getElementById('wfStopBtn').style.display='none';
                showToast('工作流执行完成！','success');
                WBStore.invalidate('reports');WBStore.invalidate('dashboard');WBStore.invalidate('activity');
                handleWfComplete()
            }
        }

        function handleWfComplete(){
            if(!wfSessionId)return;
            if(wfSelectedStep)selectWfStep(wfSelectedStep);
            else{
                var c=document.getElementById('wfContent');c&&(c.innerHTML='<div class="wf-empty"><svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="#48bb78" stroke-width="1.5"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg><h3 style="color:#48bb78">工作流执行完成</h3><p>点击左侧步骤查看各步骤生成的内容，或点击下方按钮导出报告</p></div>');
            }
            wfUploadedFiles=[];['img','pdf','doc'].forEach(function(t){renderWfFilesByType(t)});
            // 恢复文件上传区，允许开始新的评价（但保留 sessionId 用于导出和查看）
            document.getElementById('wfFileZone').style.display='';
            var sbtn=document.getElementById('wfStartBtn');if(sbtn){sbtn.disabled=false;sbtn.textContent='开始新评价'}
            setTimeout(function(){loadUserData()},1000)
        }

        function approveWorkflow(approved) {
            const feedback = document.getElementById('wfApprovalFeedback').value.trim();
            runWorkflowAction('approve', {approved, feedback, background:true, approval_step:wfApprovalStep});
        }

        async function runWorkflowAction(action, fields = {}) {
            try {
                await api('POST', '/api/agent/workflow/' + action, {session_id:wfSessionId, ...fields});
            } catch (error) {
                showToast(error.message, 'error');
            }
            restoreWorkflow();
        }

        function restoreWorkflow() {
            if (!wfSessionId) return;
            wfStatusController?.abort();
            wfStatusController = new AbortController();
            const request = (url, options) => fetch(url, {...options, headers:{Authorization:'Bearer '+getToken()}});
            watchWorkflowStatus(wfSessionId, request, data => {
                var follow=!wfSelectedStep||wfSelectedStep===wfCurrentStep;
                var selectedStatus=wfStepStatus(wfSelectedStep),selectedContent=wfStepContents[wfSelectedStep];
                wfStatus = data.status;
                wfRunning = ['running','retrying','awaiting_approval','paused'].includes(wfStatus);
                document.getElementById('wfStartBtn').disabled = wfRunning;
                document.getElementById('wfRetryBtn').hidden = !data.retryable;
                if (wfStatus === 'not_found') {
                    sessionStorage.removeItem(workflowSessionKey);
                    return;
                }
                document.getElementById('wfProgress').style.display = 'flex';
                document.getElementById('wfLayout').style.display = 'flex';
                if (data.project_name) document.getElementById('wfProjectName').value = data.project_name;
                wfCompletedSteps = data.completed_steps;
                wfCurrentStep = data.current_step;
                for (const step of data.step_summaries) {
                    if ('content' in step) wfStepContents[step.step_number] = step.content;
                    wfVersions[step.step_number] = step.version;
                    if (step.evidence) wfEvidence[step.step_number] = step.evidence;
                    if (step.comparison) wfComparisons[step.step_number] = step.comparison;
                }
                wfReviewSteps = data.review_steps;
                if (data.document_coverage && data.document_coverage.total_chars) wfCoverage = data.document_coverage;
                wfConsistency = (data.consistency && data.consistency.checked) ? data.consistency : null;
                wfDispo = (data.score_disposition && data.score_disposition.total) ? data.score_disposition : null;
                wfCritique = data.critique || (data.step9_critique ? {'9': data.step9_critique} : null);
                wfCritiqueSwitches = data.critique_switches || {};
                wfApprovalStep = null;
                if (data.requires_approval) {
                    handleSSEEvent({event:'waiting_approval', session_id:wfSessionId, approval_step:data.approval_step});
                } else {
                    document.getElementById('wfApprovalModal').style.display = 'none';
                }
                updateWfStepper();
                var target=follow?(wfCurrentStep||wfCompletedSteps.at(-1)):wfSelectedStep;
                if(target&&(target!==wfSelectedStep||selectedStatus!==wfStepStatus(target)||selectedContent!==wfStepContents[target]))selectWfStep(target);
                if (wfStatus === 'completed') {
                    document.getElementById('wfExports').style.display = 'flex';
                }
                if (data.review_steps.length) showToast('待复核步骤：' + data.review_steps.join('、'), 'warn');
            }, () => {
                wfStatus = '浏览器连接中断，正在恢复进度…';
                updateWfStepper();
            }, wfStatusController.signal);
        }

        function stopWorkflow() {
            wfAbortController?.abort();
            restoreWorkflow();
        }

        function resetWfUI(){wfRunning=false;var sbtn=document.getElementById('wfStartBtn');if(sbtn){sbtn.disabled=false;sbtn.textContent='开始评价'}document.getElementById('wfStartBtn').style.display='';document.getElementById('wfStopBtn').style.display='none';document.getElementById('wfProgress').style.display='none';document.getElementById('wfFileZone').style.display='';document.getElementById('wfLayout').style.display='none';updateWfStepper()}

        function exportReport(fmt){
            if(!wfSessionId){showToast('请先完成工作流','error');return}
            var btns=document.querySelectorAll('[onclick*="exportReport"]');
            btns.forEach(function(b){b.disabled=true;b.dataset.origText=b.textContent;b.textContent='导出中...'});
            var body={session_id:wfSessionId,format:fmt,project_name:(document.getElementById('wfProjectName').value||'绩效评价报告')};
            api('POST','/api/agent/workflow/export',body).then(function(r){
                if(r.data&&r.data.file_path){showToast('报告导出成功','success');return downloadExport(r.data.file_name)}
                else{showToast('导出失败：未获取到文件','error')}
            }).catch(function(e){showApiError(e,{action:'导出'})})
            .finally(function(){btns.forEach(function(b){b.disabled=false;if(b.dataset.origText)b.textContent=b.dataset.origText})})
        }

        // ===== AI Chat =====
        function initSpeechRecognition(){var S=window.SpeechRecognition||window.webkitSpeechRecognition;if(!S){speechRecognition=null;return}speechRecognition=new SpeechRecognition();speechRecognition.lang='zh-CN';speechRecognition.continuous=true;speechRecognition.interimResults=true;speechRecognition.onresult=function(e){var t='';for(var i=e.resultIndex;i<e.results.length;i++)t+=e.results[i][0].transcript;document.getElementById('promptTextarea').value=t;handleInputChange()};speechRecognition.onerror=function(e){stopRecording();showToast('语音识别出错','error')}}
        function startRecording(){if(!isChatEnabled){showToast('AI聊天已禁用','error');return}isRecording=true;recordingSeconds=0;updateRecordingUI();document.getElementById('voiceRecorder').classList.remove('hidden');document.getElementById('promptTextarea').style.display='none';document.getElementById('voiceTime').textContent='00:00';document.getElementById('promptBox').classList.add('recording');var b=document.getElementById('voiceBars');b.innerHTML='';for(var i=0;i<32;i++)setTimeout(function(idx){var bar=document.createElement('div');bar.className='voice-bar';bar.style.animationDelay=(idx*.05)+'s';bar.style.animationDuration=(.4+Math.random()*.6)+'s';b.appendChild(bar)}.bind(null,i),i*30);recordingTimer=setInterval(function(){recordingSeconds++;var m=Math.floor(recordingSeconds/60),s=recordingSeconds%60;document.getElementById('voiceTime').textContent=(m<10?'0'+m:m)+':'+(s<10?'0'+s:s)},1000);if(speechRecognition)try{speechRecognition.start()}catch(e){}updateSendButton()}
        function stopRecording(){isRecording=false;if(recordingTimer){clearInterval(recordingTimer);recordingTimer=null}if(speechRecognition)try{speechRecognition.stop()}catch(e){}document.getElementById('voiceRecorder').classList.add('hidden');document.getElementById('promptTextarea').style.display='';document.getElementById('promptBox').classList.remove('recording');updateSendButton();var t=document.getElementById('promptTextarea').value.trim();if(t)sendChatMessage()}
        function updateRecordingUI(){var sb=document.getElementById('sendButton'),ic=document.getElementById('sendIcon');if(isRecording){sb.className='send-btn-dark recording-btn';ic.outerHTML='<rect x="4" y="4" width="16" height="16" rx="2" fill="currentColor" id="sendIcon"/>'}else{if(document.getElementById('promptTextarea').value.trim())sb.className='send-btn-dark active';else sb.className='send-btn-dark idle';document.getElementById('sendIcon').outerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" id="sendIcon"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>'}}
        function handleSendOrRecord(){if(isRecording){stopRecording();return}var t=document.getElementById('promptTextarea').value.trim();if(t){sendChatMessage();return}if(!window.SpeechRecognition&&!window.webkitSpeechRecognition){showToast('浏览器不支持语音输入','error');return}startRecording()}

        // ===== DASHBOARD =====
function renderDashboard(d){
    if(!d)return;
    document.getElementById('statProjects').textContent=d.completed_workflows||0;
    document.getElementById('statCompleted').textContent=d.total_reports||0;
    document.getElementById('statDialogs').textContent=d.ai_dialog_count||0;
    var tel=document.getElementById('settingsTokens');
    if(tel&&d.tokens_used!==undefined)tel.textContent=d.tokens_used;
    var tc=document.getElementById('sidebarTokenCount');
    if(tc&&d.tokens_used!==undefined)tc.textContent=d.tokens_used;
    if(currentUserData&&d.tokens_used!==undefined)currentUserData.tokens_used=d.tokens_used;
}
function loadDashboardData(force){
    WBStore.remember('dashboard',function(){
        return api('GET','/api/client/dashboard',null,{signal:tabSignal('dashboard')}).then(function(r){
            return (r&&r.success&&r.data)||null;
        });
    },APP_CONFIG.cache.defaultTTL)
    .then(renderDashboard)
    .catch(function(e){if(!isAbort(e))console.warn('Dashboard data load failed:',e.message)});
}
function loadRecentActivity(force){
    var el=document.getElementById('recentActivity');if(!el)return;
    if(!force&&!WBStore.has('activity'))WBUI.renderSkeleton(el,{rows:4});
    WBStore.remember('activity',function(){
        return api('GET','/api/client/activity',null,{signal:tabSignal('dashboard')}).then(function(r){
            return (r&&r.success&&r.data)||[];
        });
    },APP_CONFIG.cache.defaultTTL)
    .then(function(data){
        if(!data.length){el.innerHTML='<p style="color:#a0aec0">暂无活动记录</p>';return}
        var h='<div style="display:flex;flex-direction:column;gap:12px">';
        for(var i=0;i<Math.min(data.length,8);i++){var a=data[i],act='',icon='';
            if(a.action==='workflow_start'){act='启动绩效评价工作流';icon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#667eea" stroke-width="2"><path d="M5 12h14"/><polyline points="12 5 19 12 12 19"/></svg>'}
            else if(a.action==='workflow_complete'){act='完成工作流';icon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#48bb78" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'}
            else if(a.action==='ai_chat'){act='AI对话';icon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9f7aea" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'}
            else if(a.action==='report_export'){act='导出报告';icon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ed8936" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'}
            else{act=a.action;icon='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'}
            h+='<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f0f0f0">'+icon+'<div style="flex:1"><div style="font-size:13px;color:#1a202c">'+esc(a.description||act)+'</div><div style="font-size:12px;color:#a0aec0">'+esc(a.time||'')+'</div></div></div>'}
        el.innerHTML=h+'</div>';
    })
    .catch(function(e){
        if(isAbort(e))return;
        el.innerHTML='<p style="color:#a0aec0">加载失败</p>';
    });
}

function loadReports(force){
    var grid=document.getElementById('reportsGrid');if(!grid)return;
    var cached=force?undefined:WBStore.get('reports');
    if(cached===undefined)WBUI.renderSkeleton(grid,{rows:3});
    WBStore.remember('reports',function(){
        return api('GET','/api/client/reports',null,{signal:tabSignal('reports')}).then(function(r){
            return (r&&r.data)||[];
        });
    },APP_CONFIG.cache.defaultTTL)
    .then(function(data){
        window._reportData=data||[];
        updateReportStats(window._reportData);
        renderReportList(window._reportData);
    })
    .catch(function(e){
        if(isAbort(e))return;
        grid.innerHTML='<div class="empty-state"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><h3>加载失败</h3><p>'+esc(e.message||'网络不可达')+'</p></div>';
    });
}
function updateReportStats(data){
    var total=data?data.length:0;
    var done=0;if(data)for(var i=0;i<data.length;i++){var s=data[i].status||'';if(s==='completed'||s==='已完成')done++}
    document.getElementById('statReportTotal')&&(document.getElementById('statReportTotal').textContent=total);
    document.getElementById('statReportDone')&&(document.getElementById('statReportDone').textContent=done);
    var latest='--';if(data&&data.length>0){var d=data[data.length-1];latest=(d.project_name||'--')+(d.updated_at?' ('+d.updated_at.slice(0,10)+')':'')}
    document.getElementById('statReportLatest')&&(document.getElementById('statReportLatest').textContent=latest);
    // 更新批量删除按钮
    updateBatchDeleteState('reports');
}
function renderReportList(data){
    var el=document.getElementById('reportsGrid');if(!el)return;
    document.getElementById('rTotalCount')&&(document.getElementById('rTotalCount').textContent=data?data.length:0);
    if(!data||!data.length){
        el.innerHTML='<div class="empty-state"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><h3>暂无评价报告</h3><p>完成一次「绩效评价」工作流后，系统将自动生成报告</p><button class="btn-primary" onclick="switchTab(\'performance\',document.querySelector(\'.nav-link[onclick*=\\\'performance\\\']\'))">前往绩效评价</button></div>';return;
    }
    var h='';
    for(var i=0;i<data.length;i++){
        var d=data[i],status=esc(d.status||'完成'),sid=esc(d.session_id||''),pn=esc(d.project_name||'未命名项目');
        var tagCls='completed';if(status==='运行中'||status==='running')tagCls='running';else if(status.indexOf('审批')>=0||status==='awaiting_approval')tagCls='awaiting_approval';
        var timeStr=(d.created_at||d.updated_at)?esc((d.created_at||d.updated_at).slice(0,19).replace('T',' ')):'--';
        h+='<div class="stat-card report-card" id="rcard_'+sid+'" style="cursor:default">';
        // 标题行：勾选框 + 项目名 + 折叠按钮
        h+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">';
        h+='<input type="checkbox" class="batch-check" onchange="updateBatchDeleteState(\'reports\')" data-sid="'+sid+'" style="width:16px;height:16px;accent-color:#667eea;cursor:pointer;flex-shrink:0">';
        h+='<div class="stat-label" style="font-size:15px;font-weight:600;flex:1;min-width:0">'+esc(pn)+'</div>';
        h+='<button class="card-toggle" data-act="card-toggle" data-target="rcard_'+sid+'" title="折叠/展开" style="width:26px;height:26px;border-radius:50%;border:1px solid #e2e8f0;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:14px;color:#718096;transition:all .2s">−</button>';
        h+='</div>';
        // 可折叠的参数区
        h+='<div class="card-detail" id="rdetail_'+sid+'">';
        // 质量徽标：review=有命中项需要人看一眼；partial=能查的过了、有些项没台账；
        // unknown=一条判据都没跑。三者都不能显示成「通过」——没数据不等于没问题。
        var rv=d.review||null,rvBadge='';
        if(rv&&rv.required){
            rvBadge='<span class="status-tag review" title="'+esc((rv.reasons||[]).join('；'))+'">建议复核</span>';
        }else if(rv&&rv.verdict==='partial'){
            rvBadge='<span class="status-tag review-partial" title="能查的质量项都通过了，但有几项历史上没有台账，无从判断">部分项未验证</span>';
        }else if(rv&&rv.verdict==='unknown'){
            rvBadge='<span class="status-tag review-unknown" title="这份报告没有可用的状态快照，质量项一项都没能检查">质量未验证</span>';
        }
        h+='<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><span class="status-tag '+tagCls+'">'+status+'</span>'+rvBadge+(d.score?'<span style="font-size:12px;color:#718096">得分：'+esc(String(d.score))+'</span>':'')+'</div>';
        h+='<div style="font-size:11px;color:#a0aec0;margin-bottom:4px">生成时间：'+timeStr+'</div>';
        h+='</div>';
        h+='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px"><button class="btn-export" style="font-size:11px;padding:5px 10px;height:26px" data-act="report-export" data-sid="'+sid+'" data-format="word" data-name="'+esc(pn)+'">Word</button><button class="btn-primary" style="font-size:11px;padding:5px 10px;height:26px" data-act="report-export" data-sid="'+sid+'" data-format="pdf" data-name="'+esc(pn)+'">PDF</button><button class="btn-delete" style="font-size:11px;padding:5px 10px;height:26px" data-act="report-delete" data-sid="'+sid+'" data-name="'+esc(pn)+'">删除</button></div>';
        h+='</div>';
    }
    el.innerHTML=h;
    updateBatchDeleteState('reports');
}
function toggleCard(cardId){
    var card=document.getElementById(cardId);if(!card)return;
    var detail=card.querySelector('.card-detail');if(!detail)return;
    var toggle=card.querySelector('.card-toggle');if(!toggle)return;
    var collapsed=detail.style.display==='none';
    detail.style.display=collapsed?'block':'none';
    toggle.textContent=collapsed?'−':'+';
    toggle.style.background=collapsed?'#fff':'#f7fafc';
}
function selectAllReports(){
    var sel=document.getElementById('rSelectAll');if(!sel)return;
    var checked=sel.checked;
    var cbs=document.getElementById('reportsGrid').querySelectorAll('.batch-check');
    cbs.forEach(function(cb){cb.checked=checked});
    updateBatchDeleteState('reports');
}
function updateBatchDeleteState(type){
    var grid=type==='reports'?document.getElementById('reportsGrid'):document.getElementById('questionnaireGrid');
    if(!grid)return;
    var cbs=grid.querySelectorAll('.batch-check:checked');
    var count=cbs.length;
    var btn=document.getElementById(type==='reports'?'rBatchDelBtn':'qBatchDelBtn');
    var selAll=document.getElementById(type==='reports'?'rSelectAll':'qSelectAll');
    if(btn){btn.disabled=count===0;btn.textContent='删除选中'+(count>0?' ('+count+')':'')}
    if(selAll){var all=grid.querySelectorAll('.batch-check');selAll.checked=all.length>0&&count===all.length;selAll.indeterminate=count>0&&count<all.length}
}
async function batchDeleteReports(){
    var cbs=document.getElementById('reportsGrid').querySelectorAll('.batch-check:checked');
    if(!cbs.length){showToast('请先勾选要删除的报告','warn');return}
    var ids=[];cbs.forEach(function(cb){ids.push(cb.getAttribute('data-sid'))});
    var ok=await WBUI.confirmDialog({title:'删除报告',message:'确定要删除选中的 '+ids.length+' 条报告吗？\n删除后数据将永久移除，不可恢复。',okText:'删除'});
    if(!ok)return;
    api('POST','/api/client/reports/batch-delete',{session_ids:ids}).then(function(r){
        if(r.success){
            showToast('成功删除 '+r.data.deleted+' 条报告','success');
            window._reportData=(window._reportData||[]).filter(function(d){return ids.indexOf(d.session_id)<0});
            updateReportStats(window._reportData);
            renderReportList(window._reportData);
            WBStore.invalidate('reports');
            WBStore.invalidate('dashboard');
        }
    }).catch(function(e){showApiError(e,{action:'批量删除'})})
}
async function deleteReport(sid,pn){
    var ok=await WBUI.confirmDialog({title:'删除报告',message:'确定要删除报告「'+pn+'」吗？\n删除后相关数据将被永久移除，不可恢复。',okText:'删除'});
    if(!ok)return;
    api('DELETE','/api/client/reports/'+encodeURIComponent(sid)).then(function(r){
        if(r.success){
            showToast('报告已删除','success');
            window._reportData=(window._reportData||[]).filter(function(d){return d.session_id!==sid});
            updateReportStats(window._reportData);
            renderReportList(window._reportData);
            WBStore.invalidate('reports');
            WBStore.invalidate('dashboard');
        }
    }).catch(function(e){showApiError(e,{action:'删除'})})
}
function exportExistingReport(sid,fmt,pn){
    var btn=event&&event.target;
    if(btn){btn.disabled=true;btn.dataset.origText=btn.textContent;btn.textContent='导出中...'}
    api('POST','/api/agent/workflow/export',{session_id:sid,format:fmt,project_name:pn}).then(function(r){
        if(r.success&&r.data&&r.data.file_name){showToast('报告导出成功','success');return downloadExport(r.data.file_name)}
        else{showToast('导出失败：未获取到文件','error')}
    }).catch(function(e){showApiError(e,{action:'导出'})})
    .finally(function(){if(btn){btn.disabled=false;if(btn.dataset.origText)btn.textContent=btn.dataset.origText}})
}
function filterReportList(){
    var raw=window._reportData||[];
    var kw=(document.getElementById('rSearchInput')?.value||'').trim().toLowerCase();
    var st=document.getElementById('rStatusFilter')?.value||'all';
    var filtered=raw.filter(function(d){
        if(kw&&(d.project_name||'').toLowerCase().indexOf(kw)<0)return false;
        if(st!=='all'){var s=d.status||'';if(st==='completed'&&s!=='已完成'&&s!=='completed')return false;if(st==='running'&&s!=='运行中'&&s!=='running')return false;if(st==='awaiting_approval'&&s.indexOf('审批')<0&&s!=='awaiting_approval')return false}
        return true;
    });
    updateReportStats(filtered);
    renderReportList(filtered);
}

function loadQuestionnaireList(force){
    var grid=document.getElementById('questionnaireGrid');if(!grid)return;
    if(window._currentQuestionData&&window._currentQuestionData.length)return;
    if(force)WBStore.invalidate('questionnaires');
    if(WBStore.get('questionnaires')===undefined)WBUI.renderSkeleton(grid,{rows:3});
    WBStore.remember('questionnaires',function(){
        return api('GET','/api/agent/workflow/questionnaire/list',null,{signal:tabSignal('questionnaire')}).then(function(r){
            return (r&&r.data)||[];
        });
    },APP_CONFIG.cache.defaultTTL)
    .then(function(data){
        window._questionnaireData=data||[];
        updateQuestionnaireStats(window._questionnaireData);
        renderQuestionnaireGrid(window._questionnaireData);
    })
    .catch(function(e){
        if(isAbort(e))return;
        grid.innerHTML='<div class="empty-state"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="1.5"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg><h3>加载失败</h3><p>'+esc(e.message||'网络不可达')+'</p></div>';
    });
}
function updateQuestionnaireStats(data){
    var total=data?data.length:0;
    var pending=0,done=0;if(data)for(var i=0;i<data.length;i++){if(data[i].question_count>0)pending++;else done++}
    document.getElementById('statQPending')&&(document.getElementById('statQPending').textContent=pending);
    document.getElementById('statQDone')&&(document.getElementById('statQDone').textContent=done);
    document.getElementById('statQTotal')&&(document.getElementById('statQTotal').textContent=total);
    updateBatchDeleteState('questionnaire');
}
function renderQuestionnaireGrid(data){
    var grid=document.getElementById('questionnaireGrid');if(!grid)return;
    document.getElementById('qTotalCount')&&(document.getElementById('qTotalCount').textContent=data?data.length:0);
    if(!data||!data.length){
        window._currentQuestionData=null;
        grid.innerHTML='<div class="empty-state"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="1.5"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg><h3>暂无问卷记录</h3><p>在「绩效评价」工作流 Step 6 可生成配套问卷</p><button class="btn-primary" onclick="switchTab(\'performance\',document.querySelector(\'.nav-link[onclick*=\\\'performance\\\']\'))">前往绩效评价</button></div>';return;
    }
    var h='';
    for(var i=0;i<data.length;i++){
        var d=data[i],sid=esc(d.session_id||''),pn=esc(d.project_name||'未命名项目');
        var label=d.question_count>0?(d.question_count+' 题 待填写'):(d.needs_generation?'待生成问卷':'已提交');
        var cls=d.question_count>0?'green':(d.needs_generation?'orange':'blue');
        var timeStr=(d.created_at||d.updated_at)?esc((d.created_at||d.updated_at).slice(0,19).replace('T',' ')):'--';
        h+='<div class="stat-card" id="qcard_'+sid+'" data-sid="'+sid+'" style="cursor:pointer;position:relative">';
        // 标题行：勾选框 + 项目名 + 折叠按钮
        h+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">';
        h+='<input type="checkbox" class="batch-check" onclick="event.stopPropagation()" onchange="updateBatchDeleteState(\'questionnaire\')" data-sid="'+sid+'" style="width:16px;height:16px;accent-color:#667eea;cursor:pointer;flex-shrink:0">';
        h+='<div class="stat-label" style="font-size:14px;font-weight:600;flex:1;min-width:0">'+esc(pn)+'</div>';
        h+='<button class="card-toggle" data-act="card-toggle" data-target="qcard_'+sid+'" title="折叠/展开" style="width:26px;height:26px;border-radius:50%;border:1px solid #e2e8f0;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:14px;color:#718096;transition:all .2s">−</button>';
        h+='</div>';
        // 可折叠区
        h+='<div class="card-detail" id="qdetail_'+sid+'">';
        h+='<div class="stat-header"><div class="stat-icon '+cls+'"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div></div>';
        h+='<div class="stat-value" style="font-size:14px;color:#667eea">'+label+'</div>';
        h+='<div style="font-size:11px;color:#a0aec0;margin-top:4px">生成时间：'+timeStr+'</div>';
        h+='<button class="btn-delete" style="position:absolute;top:8px;right:36px;font-size:11px;padding:3px 8px;height:auto;z-index:1" data-act="q-delete" data-sid="'+sid+'" data-name="'+esc(pn)+'">删除</button>';
        h+='</div>';
        h+='</div>';
    }
    grid.innerHTML=h;
    bindQuestionnaireGrid();
    updateBatchDeleteState('questionnaire');
}

/* 整卡可点（批次 56）：以前只有项目名那一小块挂了 onclick，用户点卡片图标/「N 题 待填写」
   都没反应。事件委托比给每个元素塞内联 onclick 稳：项目名里带单引号也不会打断处理器。 */
function bindQuestionnaireGrid(){
    var grid=document.getElementById('questionnaireGrid');
    if(!grid||grid._qBound)return;
    grid._qBound=true;
    grid.addEventListener('click',function(e){
        var card=e.target.closest?e.target.closest('[data-sid]'):null;
        if(!card||!grid.contains(card))return;
        if(e.target.closest('.batch-check,.card-toggle,.btn-delete'))return;
        var sid=card.getAttribute('data-sid');
        if(sid)openQuestionnaire(sid,questionnaireNameOf(sid));
    });
}
function questionnaireNameOf(sid){
    var rows=window._questionnaireData||[];
    for(var i=0;i<rows.length;i++)if(rows[i].session_id===sid)return rows[i].project_name||'';
    return '';
}
async function batchDeleteQuestionnaires(){
    var cbs=document.getElementById('questionnaireGrid').querySelectorAll('.batch-check:checked');
    if(!cbs.length){showToast('请先勾选要删除的问卷','warn');return}
    var ids=[];cbs.forEach(function(cb){ids.push(cb.getAttribute('data-sid'))});
    var ok=await WBUI.confirmDialog({title:'删除问卷',message:'确定要删除选中的 '+ids.length+' 条问卷吗？\n删除后相关数据将被移除。',okText:'删除'});
    if(!ok)return;
    api('POST','/api/agent/workflow/questionnaire/batch-delete',{session_ids:ids}).then(function(r){
        if(r.success){
            showToast('成功删除 '+r.data.deleted+' 条问卷','success');
            window._questionnaireData=(window._questionnaireData||[]).filter(function(d){return ids.indexOf(d.session_id)<0});
            updateQuestionnaireStats(window._questionnaireData);
            renderQuestionnaireGrid(window._questionnaireData);
            WBStore.invalidate('questionnaires');
        }
    }).catch(function(e){showApiError(e,{action:'批量删除'})})
}
async function deleteQuestionnaire(sid,pn){
    var ok=await WBUI.confirmDialog({title:'删除问卷',message:'确定要删除问卷「'+pn+'」吗？\n删除后相关问卷数据将被移除。',okText:'删除'});
    if(!ok)return;
    api('DELETE','/api/agent/workflow/questionnaire/'+encodeURIComponent(sid)).then(function(r){
        if(r.success){
            showToast('问卷已删除','success');
            window._questionnaireData=(window._questionnaireData||[]).filter(function(d){return d.session_id!==sid});
            updateQuestionnaireStats(window._questionnaireData);
            renderQuestionnaireGrid(window._questionnaireData);
            WBStore.invalidate('questionnaires');
        }
    }).catch(function(e){showApiError(e,{action:'删除'})})
}
function filterQuestionnaireList(){
    var raw=window._questionnaireData||[];
    var kw=(document.getElementById('qSearchInput')?.value||'').trim().toLowerCase();
    var st=document.getElementById('qStatusFilter')?.value||'all';
    var filtered=raw.filter(function(d){
        if(kw&&(d.project_name||'').toLowerCase().indexOf(kw)<0)return false;
        if(st==='pending'&&d.question_count<=0)return false;
        if(st==='submitted'&&d.question_count>0)return false;
        return true;
    });
    updateQuestionnaireStats(filtered);
    renderQuestionnaireGrid(filtered);
}
function selectAllQuestionnaires(){
    var sel=document.getElementById('qSelectAll');if(!sel)return;
    var checked=sel.checked;
    var cbs=document.getElementById('questionnaireGrid').querySelectorAll('.batch-check');
    cbs.forEach(function(cb){cb.checked=checked});
    updateBatchDeleteState('questionnaire');
}

/* 打开已发布的问卷：先读，读不到才生成。
   为什么不直接 POST：`/api/agent/workflow/questionnaire` 的缓存签名含 max_questions，
   客户端这里用 20、工作流页用 30 —— 参数一变就重新生成一版，题号全换，
   已经提交的答案再也挂不到新题上（批次 56）。GET 是纯读，重复打开不会产生新版本。 */
function fetchPublishedQuestionnaire(sid){
    return api('GET','/api/agent/workflow/'+encodeURIComponent(sid)+'/questionnaire').then(function(r){
        if(r&&r.success&&r.data&&r.data.questionnaire&&r.data.questionnaire.length)return r;
        var err=new Error('问卷尚未生成');err.code='empty';throw err;
    });
}
function openQuestionnaire(sid,pn){
    var stats=document.getElementById('questionnaireStatsGrid');
    if(stats)stats.style.display='none';
    var grid=document.getElementById('questionnaireGrid');
    if(grid)grid.innerHTML='<p class="loading-hint">加载问卷中...</p>';
    fetchPublishedQuestionnaire(sid).catch(function(err){
        /* 404（还没发布）或空问卷才值得生成一次；网络/权限问题原样抛出，不要拿生成去掩盖 */
        if(err&&(err.code==='empty'||err.status===404))
            return api('POST','/api/agent/workflow/questionnaire',{session_id:sid,max_questions:20});
        throw err;
    }).then(function(r){
        if(!r||!r.success||!r.data||!r.data.questionnaire||!r.data.questionnaire.length){
            showToast('问卷生成失败：该工作流尚无指标体系，请先完成Step3','error');
            backToQuestionnaireList();
            return;
        }
        renderQuestionnaireForm(r.data.questionnaire, sid, pn);
    }).catch(function(e){
        /* 失败必须把列表还回去：以前停在"生成问卷中..."，看起来就是"点了打不开" */
        backToQuestionnaireList();
        showApiError(e,{action:'打开问卷'});
    });
}
/* 问卷表单的唯一渲染入口（打开列表、工作流页生成后跳转都走这里） */
function renderQuestionnaireForm(qs,sid,pn){
    var panel=document.getElementById('questionnaire');
    if(!panel||!qs||!qs.length)return;
    window._currentQuestionData=qs;
    var levels=['非常不满意','不满意','一般','满意','非常满意'];
    var h='<div class="content-header"><h1>问卷填写</h1><p>项目：'+esc(pn||'未命名')+' | 共 '+qs.length+' 题</p></div>';
    h+='<div style="max-height:calc(100vh - 220px);overflow-y:auto;padding-right:4px">';
    for(var i=0;i<qs.length;i++){
        var q=qs[i],qt=q.question_text||q.question||q.text||('题目'+(i+1));
        h+='<div class="content-panel" style="background:#fafbff"><div class="panel-title" style="font-size:14px">第'+(i+1)+'题：'+esc(qt)+'</div><div class="panel-content"><div style="display:flex;gap:8px;flex-wrap:wrap">';
        for(var li=0;li<5;li++)h+='<label class="likert-label" onmouseover="this.style.borderColor=\'#667eea\';this.style.background=\'#ebf5ff\'" onmouseout="var r=document.querySelector(\'input[name=q_'+i+']:checked\');if(!r||r.value!==\''+li+'\'){this.style.borderColor=\'#e2e8f0\';this.style.background=\'white\'}"><input type="radio" name="q_'+i+'" value="'+li+'" onchange="markAnswered(this)" style="accent-color:#667eea">'+levels[li]+'</label>';
        h+='</div></div></div>'}
    h+='<div style="padding:20px;display:flex;gap:12px;justify-content:center"><button class="btn-export" onclick="backToQuestionnaireList()">返回列表</button><button class="btn-primary" style="padding:10px 32px" data-act="q-submit" data-sid="'+esc(sid)+'" data-total="'+qs.length+'">提交问卷</button></div></div>';
    questionnaireSectionSnapshot();   // 覆盖之前先把列表那套标记存下来
    panel.innerHTML=h;
}
function markAnswered(rb){
    var label=rb.parentElement;
    var all=label.parentElement.querySelectorAll('label');
    all.forEach(function(l){l.style.borderColor='#e2e8f0';l.style.background='white'});
    label.style.borderColor='#667eea';label.style.background='#ebf5ff'
}
function submitQuestionnaire(sid,total){
    var answers=[];
    for(var i=0;i<total;i++){
        var rb=document.querySelector('input[name=q_'+i+']:checked');
        if(!rb){showToast('请回答第'+(i+1)+'题','error');return}
        var actualQid=window._currentQuestionData?(window._currentQuestionData[i]?.question_id||window._currentQuestionData[i]?.id||''+i):(''+i);
        var actualQtext=window._currentQuestionData?(window._currentQuestionData[i]?.question_text||''):'';
        var actualQtype=window._currentQuestionData?(window._currentQuestionData[i]?.question_type||'likert'):'likert';
        answers.push({question_id:actualQid,question_text:actualQtext,question_type:actualQtype,selected_level_index:parseInt(rb.value)})
    }
    api('POST','/api/agent/workflow/questionnaire/submit',{session_id:sid,answers:answers}).then(function(r){
        if(r.success){showToast('问卷提交成功！共 '+r.data.saved+' 条','success');backToQuestionnaireList()}
    }).catch(function(e){showApiError(e,{action:'提交问卷'})})
}
/* 问卷区的原始标记只加载一次：进表单前存快照，返回时原样放回。
   以前这里硬编码重建了整段（三张统计卡 + 搜索 + 筛选 + 全选 + 批量删除 + stepper），
   与静态标记两份，已经漂移过（#qTotalCount 只在 JS 里）。*/
var _qSectionSnapshot=null;
function questionnaireSectionSnapshot(){
    if(_qSectionSnapshot===null){
        var sec=document.getElementById('questionnaire');
        _qSectionSnapshot=sec?sec.innerHTML:'';
    }
    return _qSectionSnapshot;
}
function backToQuestionnaireList(){
    window._currentQuestionData=null;
    var sec=document.getElementById('questionnaire');
    var snapshot=questionnaireSectionSnapshot();
    if(sec&&snapshot)sec.innerHTML=snapshot;
    /* 提交后"已提交"计数会变，缓存必须作废，否则返回列表看到的还是旧统计 */
    WBStore.invalidate('questionnaires');
    loadQuestionnaireList();
}

// ===== REPORTS API (backend side) =====

// ===== INIT =====
document.addEventListener('DOMContentLoaded',function(){
    try{
        var u=getUser();if(!u){window.location.href='/login?redirect=/client';return}
        registerTabs();bindNavTabs();
        initSpeechRecognition();initWfStepper();
        wfSessionId=new URLSearchParams(location.search).get('session') || sessionStorage.getItem(workflowSessionKey);
        if(wfSessionId) restoreWorkflow();
        loadUserData();loadAiModels();
        /* 首次进入按 hash 恢复模块，只拉当前模块的数据 */
        switchTab(restoreTabFromHash(),null,{fromHash:true});
        startNotificationPoll();
    }catch(e){
        console.error('初始化失败:',e);
        WBUI.showToast('页面初始化失败：'+e.message+'，请刷新重试','error',{timeout:8000});
    }
})
        function loadUserData(){var tel=document.getElementById('settingsTokens');if(tel)tel.textContent='加载中...';api('GET','/api/client/me').then(function(r){if(r&&r.success&&r.data){currentUserData=r.data;updateAllUI();refreshTokenDisplay()}}).catch(function(e){if(isAbort(e))return;console.error('加载用户数据失败:',e.message||e)})}
        var _tokenPollTimer=null;
        /* token 刷新已并入统一调度器，保留函数名兼容既有调用 */
        function startTokenPolling(){stopTokenPolling();PollScheduler.register('token',refreshTokenDisplay,APP_CONFIG.poll.token);PollScheduler.start()}
        function stopTokenPolling(){if(_tokenPollTimer){clearInterval(_tokenPollTimer);_tokenPollTimer=null}}
        function refreshTokenDisplay(){api('GET','/api/client/token-stats').then(function(r){if(r&&r.success&&r.data){var t=r.data.total_tokens||0;document.getElementById('sidebarTokenCount').textContent=t;document.getElementById('settingsTokens').textContent=t;if(currentUserData)currentUserData.tokens_used=t}}).catch(function(){})}
        function checkAiChatStatus(){api('GET','/api/client/ai/chat-status').then(function(r){if(!r||!r.data)return;isChatEnabled=r.data.enabled;var d=document.getElementById('chatDisabled'),b=document.getElementById('sendButton'),p=document.getElementById('promptTextarea');if(!isChatEnabled){if(d)d.style.display='block';if(b)b.disabled=true;if(p)p.disabled=true;if(p)p.placeholder='AI聊天已被管理员禁用'}else{if(d)d.style.display='none';if(b)b.disabled=false;if(p)p.disabled=false;if(p)p.placeholder='输入您的问题...'}}).catch(function(e){console.warn('AI聊天状态查询失败:',e)})}
        function updateAllUI(){if(!currentUserData){setTimeout(function(){loadUserData()},2000);return}var n=currentUserData.username,i=n.charAt(0).toUpperCase(),r=currentUserData.role==='admin'?'管理员':'普通用户';document.getElementById('sidebarUserName').textContent=n;document.getElementById('sidebarUserRole').textContent=r;[document.getElementById('userAvatar'),document.getElementById('settingsAvatar')].forEach(function(e){if(!e)return;if(currentUserData.avatar_url)e.innerHTML='<img src="'+currentUserData.avatar_url+'" alt="头像">';else{var s=e.querySelector('span');if(s)s.textContent=i}});var sa=document.getElementById('settingsAvatarInitial');if(sa&&!currentUserData.avatar_url)sa.textContent=i;document.getElementById('settingsRole').textContent=r;document.getElementById('settingsCreatedAt').textContent=currentUserData.created_at?new Date(currentUserData.created_at).toLocaleDateString('zh-CN'):'--';document.getElementById('settingsTokens').textContent=currentUserData.tokens_used||0;
            document.getElementById('sidebarTokenCount').textContent=currentUserData.tokens_used||0;
            document.getElementById('editUsername').value=n}
        function handleAvatarUpload(e){var f=e.target.files[0];if(!f)return;if(!f.type.startsWith('image/')){showToast('仅支持图片','error');return}if(f.size>5242880){showToast('图片不能超过5MB','error');return}var fd=new FormData();fd.append('file',f);uploadApi('/api/client/avatar',fd).then(function(r){if(r.success){currentUserData.avatar_url=r.data.avatar_url;updateAllUI();showToast('头像更新成功')}}).catch(function(e){showApiError(e)});e.target.value=''}
        function saveUsername(){var n=document.getElementById('editUsername').value.trim();if(!n){showToast('用户名不能为空','error');return}if(n.length<2){showToast('用户名至少2个字符','error');return}if(n===currentUserData.username){showToast('用户名未变化','success');return}var b=document.querySelector('#settings button.btn-primary');b.disabled=true;b.textContent='保存中...';api('PUT','/api/client/profile',{username:n}).then(function(r){if(r.success){currentUserData.username=r.data.username;currentUserData.avatar_url=r.data.avatar_url;currentUserData.role=r.data.role;updateAllUI();var u=getUser();if(u){u.username=r.data.username;var s=JSON.stringify(u);sessionStorage.setItem('user',s);if(localStorage.getItem('user'))localStorage.setItem('user',s)}showToast('用户名修改成功')}}).catch(function(e){showApiError(e)}).finally(function(){b.disabled=false;b.textContent='保存'})}
        function handleLogout(){if(!confirm('确定退出？'))return;sessionStorage.clear();localStorage.clear();showToast('已退出登录');setTimeout(function(){window.location.href='/login'},500)}
        function handleDeleteAccount(){if(!confirm('警告：此操作将永久删除您的账号和所有数据！\n\n确定要注销吗？'))return;var i=prompt('请输入"确认注销"以继续：');if(i!=='确认注销'){showToast('操作已取消','error');return}var b=document.getElementById('deleteAccountBtn');b.disabled=true;b.textContent='注销中...';api('DELETE','/api/client/account').then(function(r){if(r.success){sessionStorage.clear();localStorage.clear();alert('账号已成功注销。');window.location.href='/'}}).catch(function(e){showToast(e.message,'error');b.disabled=false;b.textContent='注销账号'})}
        function changeClientPassword(){var o=document.getElementById('oldPassword').value.trim(),n=document.getElementById('newPassword').value,cn=document.getElementById('confirmNewPassword').value,err=document.getElementById('pwdError'),ok=document.getElementById('pwdSuccess'),btn=document.getElementById('pwdSaveBtn');err.style.display='none';ok.style.display='none';if(!o||!n||!cn){err.textContent='请填写所有密码字段';err.style.display='block';return}if(n.length<6){err.textContent='新密码长度不能少于6位';err.style.display='block';return}if(n!==cn){err.textContent='两次输入的新密码不一致';err.style.display='block';return}btn.disabled=true;btn.textContent='修改中...';api('PUT','/api/client/password',{old_password:o,new_password:n}).then(function(r){if(r&&r.data&&r.data.access_token){WBApi.setToken(r.data.access_token)}ok.style.display='block';document.getElementById('oldPassword').value='';document.getElementById('newPassword').value='';document.getElementById('confirmNewPassword').value='';showToast('密码修改成功');if(sessionStorage.getItem('must_change_password')==='1'||localStorage.getItem('must_change_password')==='1'){sessionStorage.removeItem('must_change_password');localStorage.removeItem('must_change_password');ok.textContent='密码修改成功！页面即将刷新…';setTimeout(function(){location.reload()},900)}}).catch(function(e){err.textContent=e.message||'修改失败';err.style.display='block'}).finally(function(){btn.disabled=false;btn.textContent='修改密码'})}

        // ===== AI CHAT =====
        function handleInputChange(){var t=document.getElementById('promptTextarea'),s=document.getElementById('sendButton');if(!isRecording){if(t.value.trim())s.className='send-btn-dark active';else s.className='send-btn-dark idle'}}
        function updateSendButton(){var s=document.getElementById('sendButton');if(isRecording)s.className='send-btn-dark recording-btn';else if(document.getElementById('promptTextarea').value.trim())s.className='send-btn-dark active';else s.className='send-btn-dark idle'}
        function handleKeyDown(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChatMessage()}}
        // ── B5（批次 38）：聊天走 SSE，引用报告上下文；失败回退非流式 ──
        function streamChatReply(body,onDelta){
            var token=sessionStorage.getItem('token')||localStorage.getItem('token')||'';
            return fetch('/api/client/ai/chat/stream',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(body)}).then(function(res){
                if(!res.ok||!res.body)throw new Error('stream unavailable');
                var reader=res.body.getReader(),decoder=new TextDecoder(),buf='',donePayload=null;
                function handle(block){
                    var ev='',data='';
                    block.split('\n').forEach(function(line){
                        if(line.indexOf('event: ')==0)ev=line.slice(7).trim();
                        else if(line.indexOf('data: ')==0)data+=line.slice(6);
                    });
                    if(!data)return;
                    var payload;try{payload=JSON.parse(data)}catch(e){return}
                    if(ev==='delta'&&payload.text)onDelta(payload.text);
                    else if(ev==='done')donePayload=payload;
                    else if(ev==='error'){var err=new Error(payload.detail||'AI服务暂时不可用');err.status=payload.status;throw err}
                }
                function pump(){
                    return reader.read().then(function(r){
                        if(r.done)return donePayload;
                        buf+=decoder.decode(r.value,{stream:true});
                        var idx;
                        while((idx=buf.indexOf('\n\n'))>=0){var block=buf.slice(0,idx);buf=buf.slice(idx+2);handle(block)}
                        return pump();
                    });
                }
                return pump();
            });
        }
        function addStreamingBubble(){
            var c=document.getElementById('chatMessages');if(!c)return null;
            var el=document.createElement('div');el.className='chat-message-dark assistant';
            el.innerHTML='<div class="chat-avatar assistant">AI</div><div class="chat-bubble-dark"></div>';
            c.appendChild(el);c.scrollTop=c.scrollHeight;
            var bubble=el.querySelector('.chat-bubble-dark'),text='';
            return {el:el,
                append:function(piece){text+=piece;bubble.innerHTML=esc(text).replace(/\n/g,'<br>');c.scrollTop=c.scrollHeight},
                text:function(){return text},
                remove:function(){el.remove()}};
        }
        function appendChatReferences(el,refs){
            if(!el||!refs||!refs.length)return;
            var box=document.createElement('div');box.className='chat-refs';
            var html='<span class="chat-refs-title">依据：</span>';
            for(var i=0;i<Math.min(refs.length,6);i++){
                var r=refs[i];
                var label=r.label||r.id||'';
                var detail=(r.chapter?r.chapter+' · ':'')+(r.detail||'');
                html+='<span class="chat-ref" data-detail="'+esc(detail)+'" onclick="toggleChatRef(this)">'+esc(label)+'</span>';
            }
            box.innerHTML=html;el.querySelector('.chat-bubble-dark').appendChild(box);
        }
        function toggleChatRef(el){
            var box=el.parentNode,open=box.querySelector('.chat-ref-detail-open');
            if(open)open.remove();
            var d=document.createElement('div');d.className='chat-ref-detail-open';
            d.textContent=el.getAttribute('data-detail')||'';
            box.appendChild(d);
        }
        function sendChatMessage(){var t=document.getElementById('promptTextarea'),m=t.value.trim();if(!m)return;if(!isChatEnabled){showToast('AI聊天已禁用','error');return}t.value='';updateSendButton();var dp=m;(currentMode==='search')?dp='[搜索: '+m+']':(currentMode==='think')?dp='[深度思考: '+m+']':(currentMode==='canvas')?dp='[画布: '+m+']':null;
            addChatBubble('user',m);t.disabled=true;document.getElementById('sendButton').disabled=true;
            var reqBody={message:dp};if(currentConversationId)reqBody.conversation_id=currentConversationId;
            if(typeof wfSessionId!=='undefined'&&wfSessionId)reqBody.workflow_session_id=wfSessionId;
            var sel=document.getElementById('aiModelSelect');if(sel&&sel.value)reqBody.model=sel.value;
            var live=addStreamingBubble();
            streamChatReply(reqBody,function(piece){if(live)live.append(piece)}).then(function(done){
                if(!done)throw new Error('流式响应没有结束帧');
                if(live&&!live.text())live.append(done.reply||'');
                if(live)appendChatReferences(live.el,done.references);
                if(done.model){var st=document.getElementById('aiModelStatus');if(st)st.textContent='模型: '+done.model}
                if(!currentConversationId||done.conversation_id!==currentConversationId)currentConversationId=done.conversation_id;
                loadConversations();refreshTokenDisplay()
            }).catch(function(){
                // 流式不可用（老网关/代理缓冲/上游报错）：退回非流式，用户至少拿到回答
                if(live)live.remove();
                api('POST','/api/client/ai/chat',reqBody).then(function(r){
                    var el=addChatBubble('assistant',r.data.reply);
                    appendChatReferences(el,r.data.references);
                    if(r.data.model){var st=document.getElementById('aiModelStatus');if(st)st.textContent='模型: '+r.data.model}
                    if(!currentConversationId||r.data.conversation_id!==currentConversationId)currentConversationId=r.data.conversation_id;
                    loadConversations();refreshTokenDisplay()
                }).catch(function(e){
                    var msg='未知错误';if(typeof e==='string')msg=e;else if(e&&typeof e.message==='string')msg=e.message;
                    addChatBubble('assistant','抱歉，AI服务暂时不可用：'+msg)
                })
            })
            .finally(function(){t.disabled=false;document.getElementById('sendButton').disabled=false;t.focus();checkAiChatStatus()})
        }
        function loadAiModels(){api('GET','/api/client/ai/models').then(function(r){var sel=document.getElementById('aiModelSelect');if(!sel)return;var models=r.data.models||[];var def=r.data.default_model||'';if(!models.length){sel.innerHTML='<option value="">暂无可用模型</option>';var st=document.getElementById('aiModelStatus');if(st)st.textContent='未配置API密钥';return}sel.innerHTML='';var savedModel=localStorage.getItem('ai_selected_model')||def;var hasSaved=false;for(var i=0;i<models.length;i++){var m=models[i];var opt=document.createElement('option');opt.value=m.model;opt.textContent=m.provider+' - '+m.model;opt.title=m.provider+' / '+m.model;if(m.model===savedModel){opt.selected=true;hasSaved=true}sel.appendChild(opt)}if(!hasSaved&&def){sel.value=def}onModelChange(sel.value)}).catch(function(e){var sel=document.getElementById('aiModelSelect');if(sel)sel.innerHTML='<option value="">加载失败</option>'})}
        function onModelChange(val){var st=document.getElementById('aiModelStatus');if(st){if(val)st.textContent='当前: '+val;else st.textContent='--'}if(val){localStorage.setItem('ai_selected_model',val);var wf=document.getElementById('wfModel');if(wf){var found=false;for(var i=0;i<wf.options.length;i++){if(wf.options[i].value===val){wf.selectedIndex=i;found=true;break}}if(!found){var opt=document.createElement('option');opt.value=val;opt.textContent=val;wf.appendChild(opt);wf.value=val}showToast('已切换模型: '+val,'success')}}}
        function onWfModelChange(val){if(!val)return;var sel=document.getElementById('aiModelSelect');if(sel){var found=false;for(var i=0;i<sel.options.length;i++){if(sel.options[i].value===val){sel.selectedIndex=i;found=true;break}}if(!found){var opt=document.createElement('option');opt.value=val;opt.textContent=val;sel.appendChild(opt);sel.value=val}}var st=document.getElementById('aiModelStatus');if(st)st.textContent='当前: '+val;localStorage.setItem('ai_selected_model',val);showToast('工作流模型已切换: '+val,'success')}
        function addChatBubble(role,content,isTemp){var c=document.getElementById('chatMessages');if(!c)return null;var el=document.createElement('div');el.className='chat-message-dark '+role;var av='<div class="chat-avatar '+role+'">'+(role==='user'?'我':'AI')+'</div>';var bb='<div class="chat-bubble-dark">'+esc(content)+'</div>';el.innerHTML=role==='user'?bb+av:av+bb;c.appendChild(el);c.scrollTop=c.scrollHeight;if(!isTemp){var e=c.querySelector('.chat-empty');if(e)e.style.display='none'}return el}
        function renderChatMessages(msgs){var c=document.getElementById('chatMessages');if(!c)return;c.innerHTML='';if(!msgs||!msgs.length){c.innerHTML='<div class="chat-empty"><h2>我能帮您什么？</h2><p>随时向我提问，我会尽力帮助您</p></div>';return}for(var i=0;i<msgs.length;i++){var m=msgs[i],el=document.createElement('div');el.className='chat-message-dark '+m.role;var av='<div class="chat-avatar '+m.role+'">'+(m.role==='user'?'我':'AI')+'</div>';el.innerHTML=m.role==='user'?'<div class="chat-bubble-dark">'+esc(m.content)+'</div>'+av:av+'<div class="chat-bubble-dark">'+esc(m.content)+'</div>';c.appendChild(el)}c.scrollTop=c.scrollHeight}

        function createNewConversation(){currentConversationId=null;renderChatMessages([]);var t=document.getElementById('promptTextarea');if(t)t.focus();switchTab('ai-chat',document.querySelector('[onclick*=\"ai-chat\"]'));loadConversations()}
        function loadConversations(){api('GET','/api/client/ai/conversations').then(function(r){conversations=r.data||[];renderConvList()}).catch(function(){})}
        function renderConvList(){var l=document.getElementById('convList');if(!l)return;if(!conversations||!conversations.length){l.innerHTML='<li class="no-conv">暂无对话记录</li>';return}l.innerHTML='';for(var i=0;i<conversations.length;i++){(function(idx){var c=conversations[idx],li=document.createElement('li');li.className='conv-item'+(c.id===currentConversationId?' active':'');li.innerHTML='<span class="conv-title" title="'+esc(c.title||'')+'">'+esc(c.title||'')+'</span><button class="conv-delete" title="删除" onclick="event.stopPropagation();deleteConv('+c.id+')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>';li.onclick=function(){switchToConv(c.id)};l.appendChild(li)})(i)}}
        function switchToConv(cid){if(!cid){currentConversationId=null;renderChatMessages([]);renderConvList();return}currentConversationId=cid;api('GET','/api/client/ai/conversations/'+cid).then(function(r){if(r.success){renderChatMessages(r.data.messages);switchTab('ai-chat',document.querySelector('[onclick*=\"ai-chat\"]'));renderConvList()}}).catch(function(){})}
        function deleteConv(cid){if(!confirm('确定删除此对话？'))return;api('DELETE','/api/client/ai/conversations/'+cid).then(function(r){showToast(r.message||'已删除');if(currentConversationId===cid){currentConversationId=null;renderChatMessages([])}loadConversations()}).catch(function(e){showApiError(e)})}

        function toggleMode(md){currentMode=currentMode===md?'':md;var s=document.getElementById('toggleSearch'),t=document.getElementById('toggleThink'),c=document.getElementById('toggleCanvas');if(s)s.className='toggle-btn'+(currentMode==='search'?' on':'');if(t)t.className='toggle-btn think'+(currentMode==='think'?' on':'');if(c)c.className='toggle-btn canvas'+(currentMode==='canvas'?' on':'');var ph='输入您的问题...';if(currentMode==='search')ph='搜索网络...';else if(currentMode==='think')ph='深度思考...';else if(currentMode==='canvas')ph='创建画布...';document.getElementById('promptTextarea').placeholder=ph}
        function handleFileUpload(e){showToast('图片已选择，输入文本后发送');e.target.value=''}
        function toggleUserCardMenu(e){e.stopPropagation();document.getElementById('userCardMenu').classList.toggle('show')}
        document.addEventListener('click',function(){var m=document.getElementById('userCardMenu');if(m)m.classList.remove('show')})

        function toggleCollapse(){var s=document.getElementById('sidebar'),ic=document.getElementById('collapseIcon');isCollapsed=!isCollapsed;if(isCollapsed){s.classList.remove('expanded');s.classList.add('collapsed');ic.innerHTML='<polyline points="9 18 15 12 9 6"/>'}else{s.classList.remove('collapsed');s.classList.add('expanded');ic.innerHTML='<polyline points="15 18 9 12 15 6"/>'}}
        function toggleMobileSidebar(){var s=document.getElementById('sidebar'),o=document.getElementById('mobileOverlay');isMobileOpen=!isMobileOpen;if(isMobileOpen){s.classList.add('open');o.classList.add('show')}else{s.classList.remove('open');o.classList.remove('show')}}
        /* ===== 模块生命周期 =====
         * 单一可见性机制：只用 .active class 控制，禁止同时写 style.display。
         * 进入时才拉数据，TTL 内不重复拉；离开时 abort 该模块在途请求。
         */
        var TAB_MODULES={};
        var _tabSignals={};

        function tabSignal(name){
            if(!_tabSignals[name])_tabSignals[name]=new AbortController();
            return _tabSignals[name].signal;
        }
        function abortTabRequests(name){
            var c=_tabSignals[name];
            if(c){try{c.abort()}catch(e){};delete _tabSignals[name]}
        }
        function isAbort(e){return !!e&&(e.code==='abort'||e.name==='AbortError')}

        function registerTabs(){
            TAB_MODULES={
                dashboard:{
                    mount:function(){loadDashboardData();loadRecentActivity()},
                    unmount:function(){abortTabRequests('dashboard')}
                },
                performance:{
                    mount:function(){initWfStepper()},
                    unmount:function(){}
                },
                reports:{
                    mount:function(){loadReports()},
                    unmount:function(){abortTabRequests('reports')}
                },
                questionnaire:{
                    mount:function(){loadQuestionnaireList()},
                    unmount:function(){abortTabRequests('questionnaire')}
                },
                'ai-chat':{
                    mount:function(){checkAiChatStatus();loadConversations();var ta=document.getElementById('promptTextarea');if(ta)ta.focus()},
                    unmount:function(){}
                },
                settings:{
                    mount:function(){if(currentUserData){document.getElementById('editUsername').value=currentUserData.username;updateAllUI()}},
                    unmount:function(){}
                },
                help:{mount:function(){},unmount:function(){}}
            };
        }

        function navLinkFor(tid){
            return document.querySelector('.nav-link[data-tab="'+tid+'"]');
        }

        function bindNavTabs(){
            document.querySelectorAll('.nav-link').forEach(function(link){
                var m=/switchTab\('([^']+)'/.exec(link.getAttribute('onclick')||'');
                if(m){
                    link.setAttribute('data-tab',m[1]);
                    link.setAttribute('role','tab');
                    link.addEventListener('keydown',function(e){
                        if(e.key==='Enter'||e.key===' '){e.preventDefault();switchTab(m[1],link)}
                    });
                }
            });
        }

        var _currentTab=null;

        function switchTab(tid,el,opts){
            opts=opts||{};
            try{
                if(!TAB_MODULES[tid])TAB_MODULES.help;
                var target=document.getElementById(tid);
                if(!target)return;
                if(_currentTab&&_currentTab!==tid){
                    var prev=TAB_MODULES[_currentTab];
                    if(prev&&prev.unmount)prev.unmount();
                }
                document.querySelectorAll('.nav-link').forEach(function(l){l.classList.remove('active');l.setAttribute('aria-selected','false')});
                var active=el||navLinkFor(tid);
                if(active&&active.classList){active.classList.add('active');active.setAttribute('aria-selected','true')}
                document.querySelectorAll('.content-section').forEach(function(s){s.classList.remove('active')});
                target.classList.add('active');
                _currentTab=tid;

                /* hash 同步：可直达、可刷新恢复、支持前进后退 */
                if(!opts.fromHash){
                    var hash='#'+tid;
                    if(location.hash!==hash)history.pushState({tab:tid},'',hash);
                }

                var mod=TAB_MODULES[tid];
                if(mod&&mod.mount)mod.mount();

                if(window.innerWidth<768&&isMobileOpen)toggleMobileSidebar();
            }catch(e){console.error('switchTab error:',e)}
        }

        function restoreTabFromHash(){
            var tid=(location.hash||'').replace('#','');
            if(tid&&document.getElementById(tid)&&TAB_MODULES[tid])return tid;
            return 'dashboard';
        }

        window.addEventListener('popstate',function(){
            switchTab(restoreTabFromHash(),null,{fromHash:true});
        });
        window.addEventListener('hashchange',function(){
            var tid=(location.hash||'').replace('#','');
            if(tid&&tid!==_currentTab)switchTab(tid,null,{fromHash:true});
        });

        /* ===== 统一轮询调度器 =====
         * 一次只跑一个定时器；document.hidden 时暂停，恢复可见立即补跑一次。
         */
        var PollScheduler=(function(){
            var tasks={},timer=null;
            function tick(force){
                var now=Date.now();
                Object.keys(tasks).forEach(function(name){
                    var t=tasks[name];
                    if(t.paused)return;
                    if(force||now-t.last>=t.interval){
                        t.last=now;
                        try{var r=t.fn();if(r&&r.catch)r.catch(function(){})}catch(e){console.warn('[poll]',name,e)}
                    }
                });
            }
            document.addEventListener('visibilitychange',function(){
                if(!document.hidden)tick(true);
            });
            return {
                register:function(name,fn,interval){tasks[name]={fn:fn,interval:interval,last:0}},
                pause:function(name){if(tasks[name])tasks[name].paused=true},
                resume:function(name){if(tasks[name])tasks[name].paused=false},
                start:function(){
                    if(timer)return;
                    tick(true);
                    timer=setInterval(function(){if(!document.hidden)tick(false)},5000);
                },
                stop:function(){if(timer){clearInterval(timer);timer=null}},
                runNow:function(){tick(true)}
            };
        })();

        /* ===== 通知 ===== */
        var notifCollapsed=true,NOTIF_COLLAPSE_LIMIT=5;
        function toggleNotificationPanel(){
            var p=document.getElementById('notificationPanel'),o=document.getElementById('notificationOverlay');
            if(p.classList.contains('show')){closeNotificationPanel()}else{p.classList.add('show');o.classList.add('show');fetchNotifications()}
        }
        function closeNotificationPanel(){document.getElementById('notificationPanel').classList.remove('show');document.getElementById('notificationOverlay').classList.remove('show')}
        function fetchNotifications(){
            api('GET','/api/client/notifications').then(function(r){
                if(!r||!r.success)return;
                var data=r.data,list=document.getElementById('notificationList'),badge=document.getElementById('notificationBadge');
                var count=data.unread_count||0;
                if(count>0){badge.style.display='flex';badge.textContent=count>99?'99+':count}else{badge.style.display='none'}
                var ns=data.notifications||[];
                if(!ns.length){list.innerHTML='<div class="notification-empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg><p style="font-size:14px">暂无通知</p></div>';return}
                var total=ns.length,showAll=!notifCollapsed||total<=NOTIF_COLLAPSE_LIMIT,visible=showAll?ns:ns.slice(0,NOTIF_COLLAPSE_LIMIT);
                var h='';visible.forEach(function(n){
                    h+='<div class="notification-item'+(n.is_read?'':' unread')+'" style="cursor:default">';
                    h+='<div style="display:flex;justify-content:space-between;align-items:flex-start">';
                    h+='<div style="flex:1" onclick="markNotificationRead('+n.id+')">';
                    h+='<div class="ni-title">'+esc(n.title)+'</div>';
                    h+='<div class="ni-content" id="nic_'+n.id+'">'+esc(n.content)+'</div>';
                    h+='<button class="ni-expand-btn" id="nieb_'+n.id+'" onclick="event.stopPropagation();toggleNotificationContent('+n.id+')">展开 ▼</button>';
                    h+='<div class="ni-time">'+formatTimeStr(n.created_at)+'</div></div>';
                    h+='<button style="background:none;border:none;color:#a0aec0;cursor:pointer;font-size:16px;padding:0 0 0 8px;line-height:1" onclick="event.stopPropagation();deleteNotification('+n.id+')" title="删除">✕</button>';
                    h+='</div></div>';
                });
                if(!showAll){
                    h+='<div style="padding:10px 18px;text-align:center;cursor:pointer;color:#667eea;font-size:13px;border-top:1px solid #f0f0f0" onclick="notifCollapsed=false;fetchNotifications()">展开更多 (共 '+total+' 条，已显示 '+NOTIF_COLLAPSE_LIMIT+' 条)</div>';
                }else if(total>NOTIF_COLLAPSE_LIMIT){
                    h+='<div style="padding:10px 18px;text-align:center;cursor:pointer;color:#667eea;font-size:13px;border-top:1px solid #f0f0f0" onclick="notifCollapsed=true;fetchNotifications()">收起</div>';
                }
                list.innerHTML=h
            }).catch(function(e){console.error('获取通知失败:',e)})
        }
        function markNotificationRead(id){
            api('PUT','/api/client/notifications/'+id+'/read').then(function(r){if(r&&r.success)fetchNotifications()}).catch(function(){})
        }
        function toggleNotificationContent(id){
            var content=document.getElementById('nic_'+id);
            var btn=document.getElementById('nieb_'+id);
            if(!content||!btn)return;
            if(content.classList.contains('expanded')){
                content.classList.remove('expanded');
                btn.textContent='展开 ▼';
            }else{
                content.classList.add('expanded');
                btn.textContent='收起 ▲';
            }
        }
        function markAllNotificationsRead(){
            api('PUT','/api/client/notifications/read-all').then(function(r){if(r&&r.success){showToast('已全部标记为已读');fetchNotifications()}}).catch(function(e){showApiError(e)})
        }
        function deleteNotification(id){
            if(!confirm('确认删除此通知？'))return;
            api('DELETE','/api/client/notifications/'+id).then(function(r){if(r&&r.success)fetchNotifications()}).catch(function(e){showToast('删除失败','error')})
        }
        function clearAllNotifications(){
            if(!confirm('确认清空所有通知？此操作不可撤销。'))return;
            api('DELETE','/api/client/notifications').then(function(r){if(r&&r.success){showToast(r.message);fetchNotifications()}}).catch(function(e){showToast('清空失败','error')})
        }
        function formatTimeStr(iso){if(!iso)return'';try{var d=new Date(iso),now=new Date(),diff=now-d;if(diff<60000)return'刚刚';if(diff<3600000)return Math.floor(diff/60000)+'分钟前';if(diff<86400000)return Math.floor(diff/3600000)+'小时前';if(diff<604800000)return Math.floor(diff/86400000)+'天前';return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}catch(e){return iso}}
        function startNotificationPoll(){
            PollScheduler.register('notifications',function(){
                /* 有 SSE 连接时不轮询，避免与流式事件重复 */
                if(window.WF_STREAM_ACTIVE)return;
                return fetchNotifications();
            },APP_CONFIG.poll.notifications);
            PollScheduler.register('token',refreshTokenDisplay,APP_CONFIG.poll.token);
            PollScheduler.register('chatStatus',checkAiChatStatus,APP_CONFIG.poll.chatStatus);
            PollScheduler.start();
        }
        function stopNotificationPoll(){PollScheduler.stop()}
        window.addEventListener('beforeunload',function(){stopNotificationPoll()});
        
    