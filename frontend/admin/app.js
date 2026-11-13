/* 云海睿评 · 管理端脚本
 * 从 index.html 抽出来单独托管：页面本身是 no-store（每次都要新），
 * 而脚本走 /static 可以协商缓存 —— 重复访问不用再下这 90KB。
 */
var API_BASE='/api/admin',AUTH_API='/api/auth';
var PWD_CHANGE_REQUIRED_HEADER='X-Password-Change-Required';
var accessToken=localStorage.getItem('access_token')||localStorage.getItem('token')||sessionStorage.getItem('token')||'';
var currentPage='dashboard';

document.addEventListener('DOMContentLoaded',async function(){
    try{lucide.createIcons()}catch(e){};
    if(!accessToken){
        var p=new URLSearchParams(window.location.search);
        var t=p.get('token');
        if(t){accessToken=t;localStorage.setItem('access_token',t);localStorage.setItem('token',t);window.history.replaceState({},'','/admin')}
        else{window.location.href='/login?redirect=/admin';return}
    }
    setupNavigation();
    // 先确认自己是谁：被标记「待改初始口令」的管理员，除改密外的所有接口都会 403。
    // 直接冲仪表盘只会看到满屏「加载失败」，所以要在渲染任何页面之前先拦下来。
    var me=await api(API_BASE+'/me');
    if(me&&me.success&&me.data&&me.data.must_change_password){
        showPasswordChangeGate(me.data.username);
        return;
    }
    navigateTo((location.hash||'').replace(/^#/,'')||'dashboard');
});

function setupNavigation(){
    document.querySelectorAll('.nav-item[data-page]').forEach(function(item){
        item.addEventListener('click',function(){navigateTo(item.dataset.page)})
    });
    document.getElementById('logout-btn')&&document.getElementById('logout-btn').addEventListener('click',logout);
    document.getElementById('theme-btn')&&document.getElementById('theme-btn').addEventListener('click',toggleTheme);
    applyTheme(currentTheme());   // 同步按钮文案
    // 地址栏里的 hash 就是当前页面：这样能深链、能分享、能前进后退，
    // 刷新也不会再被弹回仪表盘（原来是纯内存状态）。
    window.addEventListener('hashchange',function(){
        var p=(location.hash||'').replace(/^#/,'');
        if(p&&p!==currentPage&&PAGE_LOADERS[p])navigateTo(p);
    });
}

// 页面注册表：原来是一长串 switch，加一页要改两处（导航项 + switch）
var PAGE_LOADERS={
    'dashboard':renderDashboard,
    'client-control':renderClientControl,
    'notifications':renderNotifications,
    'users':loadUsers,
    'models':loadModels,
    'prompts':loadPrompts,
    'knowledge':loadKnowledge,
    'templates':loadTemplates,
    'module-seq':loadModuleSequences,
    'step-config':loadStepConfig,
    'indicators':loadIndicatorTypes,
    'workflows':loadWorkflows,
    'audit':loadAuditLogs,
    'system-health':loadSystemHealth,
    'settings':loadSettings,
    'file-mgmt':loadFileManagement,
    'cleanup':loadCleanupPage
};

function navigateTo(page){
    if(!PAGE_LOADERS[page])page='dashboard';
    currentPage=page;
    // 写回 hash；hashchange 回调会看到 p===currentPage 而跳过，不会自己触发自己
    if(location.hash!=='#'+page)location.hash=page;
    document.querySelectorAll('.nav-item[data-page]').forEach(function(i){i.classList.remove('active')});
    var el=document.querySelector('.nav-item[data-page="'+page+'"]');
    if(el)el.classList.add('active');
    PAGE_LOADERS[page]();
}

// ── 主题 ──────────────────────────────────────────────
// 只切 <html data-theme>，配色全在 CSS 变量里；页面头部的内联脚本会在首帧前
// 就把属性挂上，避免先白后黑的闪烁。
function currentTheme(){
    return document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';
}
function applyTheme(theme){
    if(theme==='dark')document.documentElement.setAttribute('data-theme','dark');
    else document.documentElement.removeAttribute('data-theme');
    try{localStorage.setItem('admin_theme',theme)}catch(e){}
    var label=document.getElementById('theme-label');
    if(label)label.textContent=(theme==='dark')?'浅色模式':'暗色模式';
}
function toggleTheme(){applyTheme(currentTheme()==='dark'?'light':'dark')}

function logout(){localStorage.removeItem('access_token');localStorage.removeItem('token');localStorage.removeItem('refresh_token');sessionStorage.removeItem('token');window.location.href='/'}

// ── 统一失败对象 ───────────────────────────────────────
// 约定：api() 返回对象而不是 null。成功是后端原始响应体；失败是
// {success:false, detail, status, request_id}。这样调用方既能拿到后端真实
// 原因（detail），也不会因为 null 被 `r.detail` 读到 undefined 而丢掉信息。
function apiFailure(msg,status,payload,requestId){
    return {
        success:false,
        detail:msg||'请求失败',
        status:status||0,
        request_id:requestId||(payload&&payload.request_id)||'',
        payload:payload||null
    };
}

// 后端 detail 可能是字符串，也可能是 422 的校验错误数组
function extractDetail(data,fallback){
    if(!data||typeof data!=='object')return fallback;
    var d=data.detail;
    if(typeof d==='string'&&d.trim())return d;
    if(Array.isArray(d)&&d.length){
        return d.map(function(e){
            var loc=e&&e.loc&&e.loc.length?e.loc[e.loc.length-1]:'';
            return (loc?loc+': ':'')+((e&&e.msg)||'参数无效');
        }).join('；');
    }
    if(typeof data.message==='string'&&data.message)return data.message;
    return fallback;
}

// 列表/页面级错误占位：把真实原因渲染出来，而不是永远一句「加载失败」
function failBox(resp,fallback){
    var msg=(resp&&resp.detail)||fallback;
    var rid=resp&&resp.request_id?'<br><span style="font-size:.72rem;opacity:.75">请求号: '+esc(resp.request_id)+'</span>':'';
    return '<p style="color:#ef4444;padding:14px 4px">'+esc(msg)+rid+'</p>';
}

// ── 重试与并发去重 ────────────────────────────────────
// 只对「服务端临时故障」重试：4xx 是调用方的问题，重试只会把同一个错误重放两遍。
var API_RETRY_MAX=2;
var API_RETRY_BASE=400;
var _inflight={};

function isRetryableStatus(s){return s===408||s===425||s===429||s>=500;}

var _refreshing=null;
function refreshAccessToken(){
    if(_refreshing)return _refreshing;
    var rt=localStorage.getItem('refresh_token');
    if(!rt)return Promise.resolve(false);
    _refreshing=fetch(AUTH_API+'/refresh?refresh_token_str='+encodeURIComponent(rt),{method:'POST'})
        .then(function(r){return r.ok?r.json():null})
        .then(function(d){
            var t=d&&d.data&&d.data.access_token;
            if(!t)return false;
            accessToken=t;
            localStorage.setItem('access_token',t);
            localStorage.setItem('token',t);
            return true;
        })
        .catch(function(){return false})
        .then(function(ok){_refreshing=null;return ok});
    return _refreshing;
}

async function api(url,method,body,opts){
    method=(method||'GET').toUpperCase();
    opts=opts||{};
    var isGet=method==='GET';
    var maxRetry=isGet?(opts.retries===undefined?API_RETRY_MAX:opts.retries):0;
    // 同一时刻只有一份 GET 在飞：切页/清屏再进会连着发两次同样的请求，
    // 后发的那次直接复用前一次的结果（省一次往返，也避免两边的 setState 打架）。
    var key=isGet?(method+' '+url):'';
    if(key&&_inflight[key])return _inflight[key];

    var run=(async function(){
        var attempt=0;
        for(;;){
            var r=await _sendOnce(url,method,body,opts,false);
            if(r.success||!r.retryable||attempt>=maxRetry)return r;
            attempt++;
            await new Promise(function(res){setTimeout(res,API_RETRY_BASE*Math.pow(2,attempt-1))});
        }
    })();

    if(key){_inflight[key]=run;var clear=function(){delete _inflight[key]};run.then(clear,clear)}
    return run;
}

async function _sendOnce(url,method,body,opts,retriedAuth){
    method=(method||'GET').toUpperCase();
    opts=opts||{};
    var timeout=opts.timeout||30000;
    var headers={'Content-Type':'application/json','Authorization':'Bearer '+accessToken};
    var init={method:method,headers:headers};
    var ctrl=typeof AbortController!=='undefined'?new AbortController():null;
    var timedOut=false,timer=null;
    if(ctrl){init.signal=ctrl.signal;timer=setTimeout(function(){timedOut=true;ctrl.abort()},timeout)}
    if(body!==undefined&&body!==null)init.body=JSON.stringify(body);

    var resp;
    try{
        resp=await fetch(url,init);
    }catch(e){
        if(timer)clearTimeout(timer);
        // 超时/断网是典型的「再试一次可能就好了」
        var netFail=apiFailure(timedOut?'请求超时（'+(timeout/1000)+' 秒），请稍后重试':'网络连接失败，请检查网络',0);
        netFail.retryable=true;
        return netFail;
    }
    if(timer)clearTimeout(timer);

    // 首登未改密返回 403，但语义不是「登录失效」：
    // 不能清 token、不能跳登录页，否则用户改不了密也进不去，形成死循环。
    if(resp.status===403&&resp.headers.get(PWD_CHANGE_REQUIRED_HEADER)==='1'){
        showPasswordChangeGate();
        return apiFailure('首次登录请先修改初始密码',403);
    }

    if(resp.status===401&&!retriedAuth&&await refreshAccessToken()){
        // 直接重发本请求，不要再走 api()：那样会撞上同 URL 的去重表，
        // 拿到的是自己这次请求的 promise，等于自己等自己（死锁）。
        return _sendOnce(url,method,body,opts,true);
    }
    if(resp.status===401){
        logout();
        return apiFailure('登录状态已失效，请重新登录',401);
    }

    var text='';
    try{text=await resp.text()}catch(e){}
    var data=null;
    if(text){try{data=JSON.parse(text)}catch(e){data=null}}

    if(!resp.ok){
        var failed=apiFailure(
            extractDetail(data,'请求失败（HTTP '+resp.status+'）'),
            resp.status,data,
            resp.headers.get('X-Request-ID')||(data&&data.request_id)
        );
        failed.retryable=isRetryableStatus(resp.status);
        return failed;
    }
    if(data===null)return {success:false,detail:'服务端返回了空响应',status:resp.status};
    if(typeof data!=='object')return {success:true,data:data};
    return data;
}

// 分页控件：原来这段 for 循环在 5 个列表里各抄了一遍（连闭包写法都一模一样）。
// 顺便补上「上一页/下一页/首末页」——之前上千条数据只能一页页点。
function renderPagination(container,page,totalPages,onGo){
    if(!container)return;
    container.innerHTML='';
    if(totalPages<=1)return;
    var win=2,start=Math.max(1,page-win),end=Math.min(totalPages,page+win);

    function addBtn(label,target,opt){
        opt=opt||{};
        var b=document.createElement('button');
        b.textContent=label;
        if(opt.active)b.classList.add('active');
        if(opt.disabled)b.disabled=true;
        else b.addEventListener('click',function(){onGo(target)});
        container.appendChild(b);
    }
    function addGap(){
        var d=document.createElement('span');
        d.textContent='…';
        d.style.cssText='padding:5px 6px;color:var(--muted);font-size:.8rem';
        container.appendChild(d);
    }

    addBtn('‹ 上一页',page-1,{disabled:page<=1});
    if(start>1){addBtn('1',1);if(start>2)addGap()}
    for(var i=start;i<=end;i++)addBtn(String(i),i,{active:i===page});
    if(end<totalPages){
        if(end<totalPages-1)addGap();
        addBtn(String(totalPages),totalPages);
    }
    addBtn('下一页 ›',page+1,{disabled:page>=totalPages});
}

function showToast(msg,type){type=type||'success';var c=document.getElementById('toast-container'),t=document.createElement('div');t.className='toast toast-'+type;t.textContent=msg;c.appendChild(t);setTimeout(function(){t.remove()},3000)}

function formatTime(iso){
    if(!iso)return'-';
    try{
        var s=String(iso);
        // 不带时区的 date-time 会被浏览器按**本地时间**解析（ES 规范），
        // 而库里存的是 UTC —— 显示出来就早了 8 小时。补个 Z 纠正。
        // 后端已改成输出带 Z 的串，这里只是兜底（老接口/缓存数据仍然需要）。
        if(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)&&!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s))s+='Z';
        return new Date(s).toLocaleString('zh-CN',{hour12:false})
    }catch(e){return iso}
}

function debounce(fn,delay){var timer;return function(){var ctx=this,args=arguments;clearTimeout(timer);timer=setTimeout(function(){fn.apply(ctx,args)},delay)}}

function showModal(html){
    closeModal();
    var ov=document.createElement('div');
    ov.className='modal-overlay';
    ov.id='current-modal';
    var md=document.createElement('div');
    md.className='modal';
    md.setAttribute('role','dialog');
    md.setAttribute('aria-modal','true');
    md.innerHTML=html;
    ov.appendChild(md);
    ov.addEventListener('click',function(e){if(e.target===ov)closeModal()});
    document.body.appendChild(ov);
    // 打开后把焦点送进第一个可操作元素：键盘用户不用再 Tab 一遍才能输入
    setTimeout(function(){
        var first=ov.querySelector('input:not([type=hidden]),select,textarea,button');
        if(first&&first.focus)first.focus();
    },30);
}
function closeModal(){var m=document.getElementById('current-modal');if(m)m.remove()}

// ESC 关闭浮层。强制改密闸门（#pwd-gate）是独立浮层，不受这里影响，也不能被关掉。
document.addEventListener('keydown',function(e){
    if(e.key==='Escape'||e.keyCode===27)closeModal();
});

// ── 首登强制改密闸门 ──────────────────────────────────
// 「待改初始口令」的管理员除改密外所有接口都是 403。这个浮层是唯一出口：
// 不渲染业务页面、不发业务请求，只调 /api/admin/me/password。
// 缺了它，改密表单又挂在系统设置页里（那页要先调 /api/admin/settings → 403），
// 管理员就彻底无路可走了。
var pwdGateOpen=false;
function showPasswordChangeGate(username){
    if(pwdGateOpen)return;
    pwdGateOpen=true;
    var main=document.getElementById('main-content');
    if(main)main.innerHTML='';
    var ov=document.createElement('div');
    ov.className='modal-overlay';
    ov.id='pwd-gate';
    ov.innerHTML=
        '<div class="modal" style="min-width:440px">'+
        '<h3 style="margin-bottom:6px">🔒 请先修改初始密码</h3>'+
        '<p style="font-size:.84rem;color:var(--muted);margin-bottom:14px">'+
        (username?'账号 <strong>'+esc(username)+'</strong> ':'')+
        '仍在使用初始口令。出于安全考虑，改密之前无法访问管理端任何功能。</p>'+
        '<label>当前密码</label><input id="gate-old" type="password" placeholder="请输入当前密码" autocomplete="current-password">'+
        '<label>新密码</label><input id="gate-new" type="password" placeholder="至少 6 位" autocomplete="new-password">'+
        '<label>确认新密码</label><input id="gate-new2" type="password" placeholder="再次输入新密码" autocomplete="new-password">'+
        '<p id="gate-error" style="font-size:.82rem;color:var(--danger);margin-top:6px;display:none"></p>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">'+
        '<button class="btn" onclick="logout()">退出登录</button>'+
        '<button class="btn btn-primary" id="gate-submit" onclick="submitPasswordGate()">确认修改</button></div>'+
        '</div>';
    document.body.appendChild(ov);
    setTimeout(function(){var el=document.getElementById('gate-old');if(el)el.focus()},50);
}

async function submitPasswordGate(){
    var o=document.getElementById('gate-old').value;
    var n=document.getElementById('gate-new').value;
    var n2=document.getElementById('gate-new2').value;
    var err=document.getElementById('gate-error');
    var btn=document.getElementById('gate-submit');
    function fail(m){err.textContent=m;err.style.display='block'}
    if(!o){return fail('请输入当前密码')}
    if(n.length<6){return fail('新密码长度不能少于 6 位')}
    if(n!==n2){return fail('两次输入的新密码不一致')}
    if(n===o){return fail('新密码不能与当前密码相同')}
    err.style.display='none';
    btn.disabled=true;btn.textContent='提交中...';
    var r=await api(API_BASE+'/me/password','PUT',{old_password:o,new_password:n});
    btn.disabled=false;btn.textContent='确认修改';
    if(r&&r.success){
        // 改密会让旧令牌立即失效（后端 token_version 自增），
        // 所以先把服务端补发的新令牌存下来，再刷新页面。
        if(r.data&&r.data.access_token){
            accessToken=r.data.access_token;
            localStorage.setItem('access_token',accessToken);
            localStorage.setItem('token',accessToken);
        }
        pwdGateOpen=false;
        var gate=document.getElementById('pwd-gate');if(gate)gate.remove();
        showToast('密码修改成功');
        setTimeout(function(){location.reload()},600);
    }else{
        fail((r&&r.detail)||'修改失败');
    }
}

function esc(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function tok(n){if(!n)return'0';if(n<1000)return String(n);if(n<1e6)return (n/1e3).toFixed(1)+'K';return (n/1e6).toFixed(1)+'M'}

// ===========================
//  DASHBOARD
// ===========================
async function renderDashboard(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="empty-state"><p>加载中...</p></div>';
    // 四个请求互不依赖，串行 await 等于白等三倍时间
    var res=await Promise.all([
        api(API_BASE+'/dashboard'),
        api(API_BASE+'/token-stats'),
        api(API_BASE+'/ai-chat-stats'),
        api(API_BASE+'/settings')
    ]);
    var dash=res[0],token=res[1],chatStat=res[2],sett=res[3];
    if(!dash||!dash.success){main.innerHTML=failBox(dash,'仪表盘数据加载失败');return}
    var d=dash.data.overview,logs=dash.data.recent_operations;
    var tokData=token&&token.success?token.data:null;
    var cData=chatStat&&chatStat.success?chatStat.data:null;
    var aiEnabled=sett&&sett.success?sett.data.ai_chat_enabled:true;

    main.innerHTML=
    '<div class="page-header"><h2>📊 系统总览</h2><p>实时掌握系统运行状态与客户端使用情况</p></div>'+

    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;margin-bottom:20px">'+
    statCard2('👥 总用户',tok(d.total_users),'今日活跃 '+tok(d.active_users_today))+
    statCard2('📁 工作流',tok(d.total_projects),'运行中 '+tok(d.active_workflows)+' | 完成 '+tok(d.completed_workflows))+
    statCard2('🪙 Token消耗',tok(d.total_tokens_used),'本周 '+tok(tokData?tokData.overview.week_tokens:0))+
    (cData?statCard2('💬 AI对话',tok(cData.total_conversations),'消息 '+tok(cData.total_messages)+' | 今日活跃 '+tok(cData.active_conversations_today)):'')+
    '<div class="stat-card" style="grid-column:span 1;border:2px solid '+(aiEnabled?'var(--success)':'var(--danger)')+'">'+
    '<div style="display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:8px">'+
    '<span class="status-dot '+(aiEnabled?'online':'offline')+'"></span>'+
    '<span style="font-weight:600">AI聊天</span></div>'+
    '<div class="value" style="font-size:1.2rem;color:'+(aiEnabled?'var(--success-strong)':'var(--muted)')+'">'+(aiEnabled?'运行中':'已关闭')+'</div>'+
    '<div style="margin-top:8px;display:flex;align-items:center;justify-content:center;gap:8px">'+
    '<label class="toggle-switch" style="width:52px;height:28px"><input type="checkbox" id="dash-ai-toggle" '+(aiEnabled?'checked':'')+' onchange="toggleAIDash(this.checked)"><span class="toggle-slider" style="height:28px"></span></label>'+
    '<span style="font-size:0.72rem;color:var(--muted)">客户端+管理端</span></div></div>'+
    '</div>'+

    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">'+
    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">🪙 Token消耗趋势（近30天）</h3>'+(tokData&&tokData.daily_trend.length?renderTokenTrend(tokData.daily_trend):'<p style="color:var(--muted);font-size:0.85rem">暂无数据</p>')+'</div>'+
    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">🏆 Token消耗 Top 10 用户</h3>'+(tokData&&tokData.top_users.length?renderTopUsers(tokData.top_users):'<p style="color:var(--muted);font-size:0.85rem">暂无数据</p>')+'</div>'+
    '</div>'+

    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">📋 最近操作</h3>'+
    '<table class="table-view"><thead><tr><th>用户</th><th>操作</th><th>详情</th><th>时间</th></tr></thead><tbody>'+
    (logs||[]).map(function(l){return '<tr><td>'+esc(l.username||'-')+'</td><td><span class="badge badge-info">'+esc(l.action)+'</span></td><td>'+esc(l.detail||'-')+'</td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(l.created_at)+'</td></tr>'}).join('')+
    '</tbody></table></div>';
}

function statCard2(label,value,sub){return '<div class="stat-card"><div class="label">'+label+'</div><div class="value">'+value+'</div><div class="sub">'+sub+'</div></div>'}

function renderTokenTrend(trend){
    var max=0;trend.forEach(function(d){if(d.tokens>max)max=d.tokens});
    if(max===0)max=1;
    var h='<div class="token-bar">';
    trend.forEach(function(d){var pct=Math.max(2,(d.tokens/max)*100);h+='<div class="token-bar-item" style="height:'+pct+'%"><span class="tip">'+d.date.slice(5)+': '+tok(d.tokens)+'</span></div>'});
    h+='</div><div style="display:flex;justify-content:space-between;font-size:0.65rem;color:var(--muted);margin-top:4px"><span>'+trend[0].date.slice(5)+'</span><span>'+trend[trend.length-1].date.slice(5)+'</span></div>';
    return h
}

function renderTopUsers(users){
    var max=users[0].tokens_used||1;
    var h='';users.forEach(function(u,i){
        var pct=(u.tokens_used/max)*100;
        h+='<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:0.82rem"><span style="width:20px;color:var(--muted)">'+(i+1)+'</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(u.username)+'</span><span style="width:80px;text-align:right;font-weight:600;color:var(--accent)">'+tok(u.tokens_used)+'</span><div style="width:100px;height:6px;background:var(--border);border-radius:3px"><div style="width:'+pct+'%;height:100%;background:var(--accent);border-radius:3px"></div></div></div>'
    });
    return h
}

// ===========================
//  CLIENT CONTROL CENTER
// ===========================
async function renderClientControl(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="empty-state"><p>加载中...</p></div>';
    var res=await Promise.all([
        api(API_BASE+'/settings'),
        api(API_BASE+'/token-stats'),
        api(API_BASE+'/ai-chat-stats')
    ]);
    var sett=res[0],token=res[1],chatStat=res[2];
    if(!sett||!sett.success){main.innerHTML=failBox(sett,'系统设置加载失败');return}
    var aiEnabled=sett.data.ai_chat_enabled;
    var tokData=token&&token.success?token.data:null;
    var cData=chatStat&&chatStat.success?chatStat.data:null;

    main.innerHTML=
    '<div class="page-header"><h2>🛡️ 客户端管控中心</h2><p>集中管理客户端AI能力开关、Token配额与使用情况</p></div>'+

    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">'+

    '<div class="card" style="border:2px solid '+(aiEnabled?'var(--accent)':'var(--danger)')+'">'+
    '<h3 style="font-size:1rem;margin-bottom:12px;display:flex;align-items:center;gap:8px"><span class="status-dot '+(aiEnabled?'online':'offline')+'"></span>AI聊天服务</h3>'+
    '<div style="display:flex;align-items:center;gap:16px;margin-bottom:14px">'+
    '<label class="toggle-switch" style="width:52px;height:28px"><input type="checkbox" id="cc-ai-toggle" '+(aiEnabled?'checked':'')+' onchange="toggleAIChatCc(this.checked)"><span class="toggle-slider" style="height:28px"></span></label>'+
    '<div><div style="font-weight:600;font-size:1.1rem;color:'+(aiEnabled?'var(--accent)':'var(--danger)')+'">'+(aiEnabled?'已启用':'已禁用')+'</div>'+
    '<div style="font-size:0.8rem;color:var(--muted)">客户端\'AI助手\'功能开关</div></div></div>'+
    (cData?'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;border-top:1px solid var(--border);padding-top:12px">'+
    '<div style="text-align:center"><div style="font-size:1.3rem;font-weight:700">'+tok(cData.total_conversations)+'</div><div style="font-size:0.72rem;color:var(--muted)">总对话数</div></div>'+
    '<div style="text-align:center"><div style="font-size:1.3rem;font-weight:700">'+tok(cData.total_messages)+'</div><div style="font-size:0.72rem;color:var(--muted)">总消息数</div></div>'+
    '<div style="text-align:center"><div style="font-size:1.3rem;font-weight:700">'+tok(cData.active_conversations_today)+'</div><div style="font-size:0.72rem;color:var(--muted)">今日活跃</div></div></div>':'')+
    '</div>'+

    '<div class="card">'+
    '<h3 style="font-size:1rem;margin-bottom:12px">🪙 Token消耗概览</h3>'+
    (tokData?'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+
    '<div class="stat-card" style="padding:10px"><div class="value" style="font-size:1.2rem">'+tok(tokData.overview.total_tokens)+'</div><div class="label">累计消耗</div></div>'+
    '<div class="stat-card" style="padding:10px"><div class="value" style="font-size:1.2rem;color:var(--accent)">'+tok(tokData.overview.today_tokens)+'</div><div class="label">今日消耗</div></div>'+
    '<div class="stat-card" style="padding:10px"><div class="value" style="font-size:1.2rem">'+tok(tokData.overview.week_tokens)+'</div><div class="label">本周消耗</div></div>'+
    '<div class="stat-card" style="padding:10px"><div class="value" style="font-size:1.2rem">'+tok(tokData.overview.month_tokens)+'</div><div class="label">本月消耗</div></div>'+
    '</div>':'<p style="color:var(--muted)">暂无数据</p>')+
    '</div></div>'+

    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">'+
    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">🪙 Token消耗趋势（近30天）</h3>'+(tokData&&tokData.daily_trend.length?renderTokenTrend(tokData.daily_trend):'<p style="color:var(--muted);font-size:0.85rem">暂无数据</p>')+'</div>'+
    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">🤖 模型使用分布</h3>'+(tokData&&tokData.model_usage.length?'<table class="table-view"><thead><tr><th>模型</th><th>Token</th><th>调用次数</th></tr></thead><tbody>'+tokData.model_usage.map(function(m){return '<tr><td>'+esc(m.model)+'</td><td>'+tok(m.tokens)+'</td><td>'+m.calls+'</td></tr>'}).join('')+'</tbody></table>':'<p style="color:var(--muted);font-size:0.85rem">暂无数据</p>')+'</div>'+
    '</div>'+

    '<div class="card"><h3 style="font-size:0.95rem;margin-bottom:12px">🏆 Token消耗 Top 10 用户</h3>'+(tokData&&tokData.top_users.length?renderTopUsers(tokData.top_users):'<p style="color:var(--muted)">暂无数据</p>')+'</div>';
}

async function toggleAIChatCc(enabled){
    var resp=await api(API_BASE+'/settings/ai-chat-toggle','POST',{ai_chat_enabled:enabled});
    if(resp&&resp.success){showToast(resp.message);navigateTo('client-control')}else showToast((resp&&resp.detail)||'操作失败','error');
}

async function toggleAIDash(enabled){
    var resp=await api(API_BASE+'/settings/ai-chat-toggle','POST',{ai_chat_enabled:enabled});
    if(resp&&resp.success){showToast(resp.message);navigateTo('dashboard')}else{showToast((resp&&resp.detail)||'操作失败','error');document.getElementById('dash-ai-toggle').checked=!enabled}
}

// ===========================
//  USERS (Enhanced with Token sorting)
// ===========================
var usersPage=1,usersSearch='',usersRole='',usersSort='';
async function loadUsers(){
    var main=document.getElementById('main-content');
    main.innerHTML=
        '<div class="page-header"><h2>👥 用户管理</h2><p>管理系统用户账号、权限与Token消耗</p></div>'+
        '<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">'+
        '<input id="user-search" placeholder="搜索用户名..." style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);width:160px;font-size:0.85rem">'+
        '<select id="user-role-filter" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem"><option value="">全部角色</option><option value="admin">管理员</option><option value="user">普通用户</option></select>'+
        '<select id="user-sort" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem"><option value="">默认排序</option><option value="tokens_desc">Token最多</option><option value="tokens_asc">Token最少</option></select>'+
        '<button class="btn btn-primary" onclick="showCreateUserModal()">+ 创建用户</button>'+
        '</div>'+
        '<div id="users-table-container" class="card"><div class="empty-state">加载中...</div></div>'+
        '<div id="users-pagination" class="pagination"></div>';
    document.getElementById('user-search').addEventListener('input',debounce(function(){usersSearch=this.value;usersPage=1;refreshUsers()},300));
    document.getElementById('user-role-filter').addEventListener('change',function(){usersRole=this.value;usersPage=1;refreshUsers()});
    document.getElementById('user-sort').addEventListener('change',function(){usersSort=this.value;usersPage=1;refreshUsers()});
    await refreshUsers();
}

async function refreshUsers(){
    var params=new URLSearchParams({page:usersPage,page_size:20});
    if(usersSearch)params.set('search',usersSearch);
    if(usersRole)params.set('role',usersRole);
    if(usersSort)params.set('sort',usersSort);
    var resp=await api(API_BASE+'/users?'+params);
    var c=document.getElementById('users-table-container');
    var pagDiv=document.getElementById('users-pagination');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var users=resp.data.users,total=resp.data.total,page=resp.data.page,ps=resp.data.page_size;
    // 排序由后端做（sort 参数）。前端排只能排当前页，翻页后答案就变了。
    var tp=Math.ceil(total/ps);
    c.innerHTML='<table class="table-view"><thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>🪙 Token</th><th>最近登录</th><th>注册时间</th><th>操作</th></tr></thead><tbody>'+
    users.map(function(u){return '<tr><td>'+u.id+'</td><td><strong>'+esc(u.username)+'</strong></td><td><span class="badge '+(u.role==='admin'?'badge-warning':'badge-info')+'">'+u.role+'</span></td><td><span class="badge '+(u.is_active?'badge-success':'badge-danger')+'">'+(u.is_active?'启用':'禁用')+'</span></td><td style="font-weight:600;color:'+((u.tokens_used||0)>10000?'var(--warning)':'var(--accent)')+'">'+tok(u.tokens_used)+'</td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(u.last_login)+'</td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(u.created_at)+'</td><td><button class="btn btn-sm" data-act="user-detail" data-uid="'+u.id+'">详情</button><button class="btn btn-sm '+(u.is_active?'btn-danger':'')+'" data-act="user-toggle" data-uid="'+u.id+'" data-enable="'+(!u.is_active)+'">'+(u.is_active?'禁用':'启用')+'</button>'+(u.role!=='admin'?'<button class="btn btn-sm btn-danger" data-act="user-deactivate" data-uid="'+u.id+'" style="background:#c53030;color:#fff;border:none">注销</button>':'')+'</td></tr>'}).join('')+
    '</tbody></table>';
    renderPagination(pagDiv,page,tp,function(p){usersPage=p;refreshUsers()});
}

// ── 统一事件委托 ──────────────────────────────────────
// 原来这块挂了三四个 document 监听器，各认一套 data-*；而「把字符串拼进 onclick」
// 是这里唯一真正的正确性风险 —— 引号和反斜杠都得手工转义，漏一个就整行按钮失效。
// 现在统一成 data-act + 一张调度表：参数走 dataset，不再经过 HTML 属性里的 JS 字符串。
var ACTIONS={
    'user-detail':function(el){viewUserDetail(Number(el.dataset.uid))},
    'user-toggle':function(el){toggleUser(Number(el.dataset.uid), el.dataset.enable==='true')},
    'user-deactivate':function(el){
        var row=el.closest('tr');
        var nameEl=row?row.querySelector('td:nth-child(2) strong'):null;
        deactivateUser(Number(el.dataset.uid), nameEl?nameEl.textContent:('用户#'+el.dataset.uid));
    },
    'user-chpwd-submit':function(el){changeUserPassword(Number(el.dataset.uid))},
    'user-change-pwd':function(el){
        closeModal();
        showChangePasswordModal(Number(el.dataset.uid), el.dataset.name||'');
    },
    'wf-detail':function(el){showWorkflowDetail(el.dataset.sid)},
    'wf-step':function(el){showWorkflowContent(Number(el.dataset.step))},
    'wf-fail':function(el){failWorkflow(el.dataset.sid)}
};

document.addEventListener('click', function(e){
    var el=e.target.closest('[data-act]');
    if(!el)return;
    var fn=ACTIONS[el.dataset.act];
    if(fn)fn(el);
});

async function viewUserDetail(userId){
    var resp=await api(API_BASE+'/users/'+userId);
    if(!resp||!resp.success)return;
    var u=resp.data;
    showModal('<h3 style="margin-bottom:14px">用户详情: '+esc(u.username)+'</h3>'+
        '<p><strong>ID:</strong> '+u.id+'</p>'+
        '<p><strong>角色:</strong> <span class="badge '+(u.role==='admin'?'badge-warning':'badge-info')+'">'+u.role+'</span></p>'+
        '<p><strong>状态:</strong> <span class="badge '+(u.is_active?'badge-success':'badge-danger')+'">'+(u.is_active?'启用':'禁用')+'</span></p>'+
        '<p><strong>手机:</strong> '+(u.phone||'-')+'</p>'+
        '<p><strong>🔑 口令状态:</strong> '+(u.must_change_password?'<span class="badge badge-warning">仍为初始口令，待本人修改</span>':'<span class="badge badge-success">已由本人设置</span>')+'</p>'+
        '<p><strong>🪙 Token消耗:</strong> <span style="color:var(--accent);font-weight:700">'+tok(u.tokens_used)+'</span></p>'+
        '<p><strong>项目数:</strong> '+(u.project_count||0)+'</p>'+
        '<p><strong>最近登录:</strong> '+formatTime(u.last_login)+'</p>'+
        '<p><strong>注册时间:</strong> '+formatTime(u.created_at)+'</p>'+
        '<h4 style="margin-top:12px;margin-bottom:6px">最近操作</h4>'+
        '<ul style="list-style:none;padding:0">'+(u.recent_operations||[]).map(function(o){return '<li style="font-size:0.82rem;color:var(--muted)">'+esc(o.action)+' - '+esc(o.detail||'')+' ('+formatTime(o.time)+')</li>'}).join('')+'</ul>'+
        '<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border);display:flex;gap:8px">'+
        '<button class="btn btn-primary" data-act="user-change-pwd" data-uid="'+u.id+'" data-name="'+esc(u.username)+'">🔒 修改密码</button>'+
        '<button class="btn" onclick="closeModal()">关闭</button></div>');
}

async function toggleUser(userId,enable){var resp=await api(API_BASE+'/users/'+userId,'PUT',{is_active:enable});if(resp&&resp.success){showToast(resp.message||'操作成功');refreshUsers()}else{showToast(resp&&resp.detail||'操作失败','error')}}

async function deactivateUser(userId,userName){
    if(!confirm('确认注销用户 "'+userName+'" 吗？\n\n此操作将彻底删除该用户及其所有关联数据（报告、问卷、项目、Token记录等），不可恢复！'))return;
    try{
        var resp=await api(API_BASE+'/users/'+userId+'/deactivate','POST');
        if(resp&&resp.success){showToast(resp.message||'用户已注销');refreshUsers()}
        else{showToast((resp&&resp.detail)||'注销失败（未知错误）','error')}
    }catch(e){
        showToast('注销请求失败: '+(e.message||'网络错误'),'error');
    }
}

function showCreateUserModal(){
    showModal('<h3 style="margin-bottom:12px">创建新用户</h3>'+
        '<label>用户名</label><input id="new-username" placeholder="请输入用户名">'+
        '<label>密码</label><input id="new-password" type="password" placeholder="请输入密码">'+
        '<label>手机号（选填）</label><input id="new-phone" placeholder="请输入手机号">'+
        '<label>角色</label><select id="new-role"><option value="user">普通用户</option><option value="admin">管理员</option></select>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="createUser()">创建</button></div>');
}

async function createUser(){
    var username=document.getElementById('new-username').value.trim(),password=document.getElementById('new-password').value;
    var phone=document.getElementById('new-phone').value.trim()||null,role=document.getElementById('new-role').value;
    if(!username||!password){showToast('请填写用户名和密码','error');return}
    var createBtn=document.querySelector('[onclick="createUser()"]');
    if(createBtn){createBtn.disabled=true;createBtn.dataset.origText=createBtn.textContent;createBtn.textContent='创建中...'}
    var resp=await api(API_BASE+'/users','POST',{username:username,password:password,phone:phone,role:role});
    if(createBtn){createBtn.disabled=false;if(createBtn.dataset.origText)createBtn.textContent=createBtn.dataset.origText}
    if(resp&&resp.success){showToast('创建成功');closeModal();refreshUsers()}else{showToast(resp&&resp.detail||'创建失败','error')}
}

async function showChangePasswordModal(userId,userName){
    showModal('<h3 style="margin-bottom:12px">修改密码: '+esc(userName)+'</h3>'+
        '<label>新密码</label><input id="change-pwd" type="password" placeholder="请输入新密码" style="width:100%">'+
        '<label>确认新密码</label><input id="change-pwd2" type="password" placeholder="请再次输入新密码" style="width:100%">'+
        '<p id="pwd-error" style="font-size:0.8rem;color:#ef4444;margin-top:6px;display:none"></p>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">'+
        '<button class="btn" onclick="closeModal()">取消</button>'+
        '<button class="btn btn-primary" data-act="user-chpwd-submit" data-uid="'+userId+'">确认修改</button></div>');
}

async function changeUserPassword(userId){
    var pwd=document.getElementById('change-pwd').value;
    var pwd2=document.getElementById('change-pwd2').value;
    var errEl=document.getElementById('pwd-error');
    if(!pwd||pwd.length<6){errEl.textContent='密码至少6位';errEl.style.display='block';return}
    if(pwd!==pwd2){errEl.textContent='两次密码不一致';errEl.style.display='block';return}
    var resp=await api(API_BASE+'/users/'+userId,'PUT',{password:pwd});
    if(resp&&resp.success){showToast('密码修改成功');closeModal();refreshUsers()}else{showToast(resp&&resp.detail||'密码修改失败','error')}
}

// ===========================
//  MODELS (simplified - keep existing)
// ===========================
async function loadModels(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>🧠 模型管理</h2><p>配置AI模型平台、API密钥与开关</p></div>'+
        '<div style="display:flex;gap:10px;margin-bottom:14px"><button class="btn btn-primary" onclick="showModelModal()">+ 添加模型</button></div>'+
        '<div id="models-container" class="card"><div class="empty-state">加载中...</div></div>';
    await refreshModels();
}
async function refreshModels(){
    var resp=await api(API_BASE+'/models'),c=document.getElementById('models-container');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var models=resp.data;
    c.innerHTML='<table class="table-view"><thead><tr><th>ID</th><th>平台</th><th>模型名称</th><th>API Key</th><th>状态</th><th>操作</th></tr></thead><tbody>'+
    models.map(function(m){return '<tr><td>'+m.id+'</td><td>'+esc(m.platform)+'</td><td><strong>'+esc(m.model_name)+'</strong></td><td style="font-family:monospace;font-size:0.75rem">'+(m.api_key||'-')+'</td><td><label class="toggle-switch"><input type="checkbox" '+(m.is_enabled?'checked':'')+' onchange="toggleModel('+m.id+',this.checked)"><span class="toggle-slider"></span></label></td><td><button class="btn btn-sm" onclick="showModelModal('+m.id+')">编辑</button><button class="btn btn-sm btn-danger" onclick="deleteModel('+m.id+')">删除</button></td></tr>'}).join('')+
    '</tbody></table>';
}
function showModelModal(id){
    var isEdit=!!id;
    showModal('<h3 style="margin-bottom:12px">'+(isEdit?'编辑模型':'添加模型')+'</h3>'+
        '<label>平台</label><input id="m-platform" placeholder="如: dashscope / openai">'+
        '<label>模型名称</label><input id="m-name" placeholder="如: qwen-plus">'+
        '<label>模型ID</label><input id="m-id" placeholder="如: qwen-plus">'+
        '<label>API Key</label><input id="m-key" type="password" placeholder="sk-...">'+
        '<label>API Base URL</label><input id="m-url" placeholder="如: https://dashscope.aliyuncs.com/...">'+
        '<div class="form-row"><div><label>支持图像</label><select id="m-vision"><option value="0">否</option><option value="1">是</option></select></div><div><label>支持文件</label><select id="m-file"><option value="0">否</option><option value="1">是</option></select></div></div>'+
        '<label>启用</label><select id="m-enabled"><option value="1">是</option><option value="0">否</option></select>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="saveModel('+(id||0)+')">保存</button></div>');
    if(isEdit)api(API_BASE+'/models').then(function(r){if(r&&r.data){var m=r.data.find(function(x){return x.id===id});if(!m)return;document.getElementById('m-platform').value=m.platform||'';document.getElementById('m-name').value=m.model_name||'';document.getElementById('m-id').value=m.model_id||'';document.getElementById('m-url').value=m.api_base_url||'';document.getElementById('m-vision').value=m.supports_vision?'1':'0';document.getElementById('m-file').value=m.supports_file?'1':'0';document.getElementById('m-enabled').value=m.is_enabled?'1':'0'}}).catch(function(e){console.warn('加载模型信息失败:',e);showToast('加载模型信息失败','error')});
}
async function saveModel(id){
    var data={platform:document.getElementById('m-platform').value.trim(),model_name:document.getElementById('m-name').value.trim(),model_id:document.getElementById('m-id').value.trim(),api_key:document.getElementById('m-key').value.trim(),api_base_url:document.getElementById('m-url').value.trim(),supports_vision:document.getElementById('m-vision').value==='1',supports_file:document.getElementById('m-file').value==='1',is_enabled:document.getElementById('m-enabled').value==='1'};
    if(!data.platform||!data.model_name){showToast('请填写平台和模型名称','error');return}
    var saveBtn=document.querySelector('[onclick="saveModel('+(id||0)+')"]');
    if(saveBtn){saveBtn.disabled=true;saveBtn.dataset.origText=saveBtn.textContent;saveBtn.textContent='保存中...'}
    var resp=id>0?await api(API_BASE+'/models/'+id,'PUT',data):await api(API_BASE+'/models','POST',data);
    if(saveBtn){saveBtn.disabled=false;if(saveBtn.dataset.origText)saveBtn.textContent=saveBtn.dataset.origText}
    if(resp&&resp.success){showToast(resp.message||'保存成功');closeModal();refreshModels()}else{showToast(resp&&resp.detail||'保存失败','error')}
}
async function toggleModel(id,e){var r=await api(API_BASE+'/models/'+id+'/toggle','PATCH',{is_enabled:e});if(r&&r.success)showToast(r.message);else{showToast((r&&r.detail)||'操作失败','error');refreshModels()}}
async function deleteModel(id){if(!confirm('确定删除？'))return;var r=await api(API_BASE+'/models/'+id,'DELETE');if(r&&r.success){showToast('已删除');refreshModels()}else{showToast(r&&r.detail||'删除失败','error')}}

// ===========================
//  KNOWLEDGE / TEMPLATES / INDICATORS
//  (Prompt管理已升级为细粒度版本，见文件末尾)
// ===========================

// Knowledge base
var kbPage=1,kbStep='',kbCat='';
async function loadKnowledge(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📚 知识库</h2><p>管理RAG检索增强的专业知识条目</p></div>'+
        '<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap"><select id="kb-step-filter" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem"><option value="">全部步骤</option>'+[1,2,3,4,5,6,7,8,9,10,11,12,13,14].map(function(i){return '<option value="'+i+'">Step '+i+'</option>'}).join('')+'</select>'+
        '<input id="kb-cat-filter" placeholder="分类筛选..." style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);width:130px;font-size:0.85rem">'+
        '<button class="btn btn-primary" onclick="showKnowledgeModal()">+ 添加条目</button></div>'+
        '<div id="kb-container" class="card"><div class="empty-state">加载中...</div></div><div id="kb-pagination" class="pagination"></div>';
    document.getElementById('kb-step-filter').addEventListener('change',function(){kbStep=this.value;kbPage=1;refreshKnowledge()});
    document.getElementById('kb-cat-filter').addEventListener('input',debounce(function(){kbCat=this.value;kbPage=1;refreshKnowledge()},300));
    await refreshKnowledge();
}
async function refreshKnowledge(){
    var params=new URLSearchParams({page:kbPage,page_size:15});if(kbStep)params.set('step_number',kbStep);if(kbCat)params.set('category',kbCat);
    var resp=await api(API_BASE+'/knowledge?'+params),c=document.getElementById('kb-container'),pagDiv=document.getElementById('kb-pagination');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var items=resp.data.items,total=resp.data.total,page=resp.data.page,ps=resp.data.page_size,tp=Math.ceil(total/ps);
    c.innerHTML='<table class="table-view"><thead><tr><th>ID</th><th>名称</th><th>分类</th><th>步骤</th><th>内容</th><th>状态</th><th>操作</th></tr></thead><tbody>'+items.map(function(k){return '<tr><td>'+k.id+'</td><td><strong>'+esc(k.name)+'</strong></td><td><span class="badge badge-info">'+esc(k.category||'-')+'</span></td><td>'+(k.step_number||'-')+'</td><td><div class="kb-preview">'+esc(k.content)+'</div></td><td><span class="badge '+(k.is_active?'badge-success':'badge-danger')+'">'+(k.is_active?'启用':'禁用')+'</span></td><td><button class="btn btn-sm" onclick="showKnowledgeModal('+k.id+')">编辑</button><button class="btn btn-sm btn-danger" onclick="deleteKnowledge('+k.id+')">删除</button></td></tr>'}).join('')+'</tbody></table>';
    renderPagination(pagDiv,page,tp,function(p){kbPage=p;refreshKnowledge()});
}
function showKnowledgeModal(id){
    var isEdit=!!id;
    showModal('<h3 style="margin-bottom:12px">'+(isEdit?'编辑':'添加')+'知识库条目</h3>'+
        '<label>名称</label><input id="kb-name" placeholder="条目名称"><label>分类</label><input id="kb-category" placeholder="如: 绩效管理">'+
        '<div class="form-row"><div><label>关联步骤</label><input id="kb-step" type="number" min="1" max="14"></div><div><label>指标层级</label><input id="kb-level" type="number" min="1" max="3"></div></div>'+
        '<label>内容</label><textarea id="kb-content" placeholder="知识库内容..." style="min-height:150px"></textarea>'+
        '<label>状态</label><select id="kb-active"><option value="1">启用</option><option value="0">禁用</option></select>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="saveKnowledge('+(id||0)+')">保存</button></div>');
    if(isEdit)api(API_BASE+'/knowledge/'+id).then(function(r){if(!r||!r.success)return;var k=r.data;document.getElementById('kb-name').value=k.name||'';document.getElementById('kb-category').value=k.category||'';document.getElementById('kb-step').value=k.step_number||'';document.getElementById('kb-level').value=k.indicator_level||'';document.getElementById('kb-content').value=k.content||'';document.getElementById('kb-active').value=k.is_active?'1':'0';});
}
async function saveKnowledge(id){
    var data={name:document.getElementById('kb-name').value.trim(),category:document.getElementById('kb-category').value.trim()||null,step_number:parseInt(document.getElementById('kb-step').value)||null,indicator_level:parseInt(document.getElementById('kb-level').value)||null,content:document.getElementById('kb-content').value.trim(),is_active:document.getElementById('kb-active').value==='1'};
    if(!data.name||!data.content){showToast('请填写名称和内容','error');return}
    var resp=id>0?await api(API_BASE+'/knowledge/'+id,'PUT',data):await api(API_BASE+'/knowledge','POST',data);
    if(resp&&resp.success){showToast('保存成功');closeModal();refreshKnowledge()}else{showToast(resp&&resp.detail||'保存失败','error')}
}
async function deleteKnowledge(id){if(!confirm('确定删除？'))return;var r=await api(API_BASE+'/knowledge/'+id,'DELETE');if(r&&r.success){showToast('已删除');refreshKnowledge()}else{showToast(r&&r.detail||'删除失败','error')}}

// Templates
var templateSections=[{id:'cover',name:'封面',order:1,enabled:true},{id:'toc',name:'目录',order:2,enabled:true},{id:'step12',name:'基本情况',order:3,enabled:true},{id:'step1',name:'资料清单',order:4,enabled:true},{id:'step2',name:'核心数据提取',order:5,enabled:true},{id:'step6',name:'指标体系表',order:6,enabled:true},{id:'step7',name:'得分分析',order:7,enabled:true},{id:'step8',name:'经验做法',order:8,enabled:true},{id:'step9',name:'问题分析',order:9,enabled:true},{id:'step10',name:'改进建议',order:10,enabled:true},{id:'step11',name:'综合评价',order:11,enabled:true},{id:'step13',name:'工作简介',order:12,enabled:true},{id:'step14',name:'附录',order:13,enabled:true}];
function loadTemplates(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📄 报告模板管理</h2><p>管理最终报告章节顺序与显示开关</p></div>'+
        '<div class="card"><p style="font-size:0.85rem;color:var(--muted);margin-bottom:14px">拖拽调整章节排列顺序，控制最终报告中各模块的呈现</p><div id="template-sections" style="display:flex;flex-direction:column;gap:6px"></div>'+
        '<div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end"><button class="btn btn-primary" onclick="saveTemplateOrder()">💾 保存模板</button><button class="btn" onclick="exportAdminCSV()">📥 导出用户CSV</button></div></div>';
    var sdiv=document.getElementById('template-sections');
    templateSections.sort(function(a,b){return a.order-b.order}).forEach(function(s,i){
        sdiv.innerHTML+='<div style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--card);border:1px solid var(--border);border-radius:7px"><span style="color:var(--muted);font-size:0.78rem">'+s.order+'</span><span style="flex:1;font-size:0.9rem">'+esc(s.name)+'</span>'+
        '<label class="toggle-switch"><input type="checkbox" '+(s.enabled?'checked':'')+' onchange="templateSections['+i+'].enabled=this.checked"><span class="toggle-slider"></span></label>'+
        '<button class="btn btn-sm" onclick="moveTemplateSection('+i+',-1)" '+(i===0?'disabled':'')+'>▲</button><button class="btn btn-sm" onclick="moveTemplateSection('+i+',1)" '+(i===templateSections.length-1?'disabled':'')+'>▼</button></div>'});
}
function moveTemplateSection(idx,dir){var ni=idx+dir;if(ni<0||ni>=templateSections.length)return;var to=templateSections[idx].order;templateSections[idx].order=templateSections[ni].order;templateSections[ni].order=to;loadTemplates()}
function saveTemplateOrder(){templateSections.forEach(function(s,i){s.order=i+1});var data=templateSections.map(function(s){return{id:s.id,name:s.name,order:s.order,enabled:s.enabled}});try{localStorage.setItem('admin_template_order',JSON.stringify(data));showToast('模板顺序已保存')}catch(e){showToast('保存失败','error')}}
// CSV 单元格：Excel 会把 = + - @ 开头的值当公式执行（CSV 注入），加前导单引号掐掉
function csvCell(v){
    var s=(v===null||v===undefined)?'':String(v);
    if(/^[=+\-@\t\r]/.test(s))s="'"+s;
    if(/[",\r\n]/.test(s))s='"'+s.replace(/"/g,'""')+'"';
    return s;
}
async function exportAdminCSV(){
    // 后端 page_size 上限是 100（le=100），传 9999 会 422。
    // 这里按 100 一页翻到底，最后一次拼成一个文件。
    var all=[],page=1,guard=0,pageSize=100,total=0;
    while(guard++<500){
        var resp=await api(API_BASE+'/users?page='+page+'&page_size='+pageSize);
        if(!resp||!resp.success){showToast((resp&&resp.detail)||'获取用户数据失败','error');return}
        var batch=resp.data.users||[];
        total=resp.data.total||0;
        all=all.concat(batch);
        if(!batch.length||all.length>=total)break;
        page++;
    }
    if(!all.length){showToast('暂无用户数据','error');return}
    var rows=[['ID','用户名','角色','状态','Token用量','手机号','最近登录','注册时间']];
    all.forEach(function(u){
        rows.push([u.id,u.username,u.role,u.is_active?'启用':'禁用',u.tokens_used||0,u.phone||'',
            formatTime(u.last_login),formatTime(u.created_at)]);
    });
    var csv=rows.map(function(r){return r.map(csvCell).join(',')}).join('\r\n');
    var blob=new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8'});
    var url=URL.createObjectURL(blob);
    var a=document.createElement('a');
    a.href=url;
    a.download='users_export_'+new Date().toISOString().slice(0,10)+'.csv';
    a.click();
    URL.revokeObjectURL(url);
    showToast('已导出 '+all.length+' 个用户');
}

// Indicators
var indicatorTypes=[{id:'budget',name:'项目支出绩效评价',desc:'通用财政预算项目绩效评价',active:true},{id:'procurement',name:'政府采购绩效评价',desc:'政府采购项目绩效评价',active:false},{id:'service',name:'政府购买服务绩效评价',desc:'政府购买第三方服务绩效评价',active:false},{id:'bond',name:'专项债绩效评价',desc:'政府专项债券项目绩效评价',active:false},{id:'social',name:'社保基金绩效评价',desc:'社会保险基金运行绩效评价',active:false},{id:'asset',name:'国有资产绩效评价',desc:'国有资本经营项目绩效评价',active:false}];
function loadIndicatorTypes(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📊 指标类型管理</h2><p>管理绩效评价指标体系的项目分类（客户端可选项）</p></div>'+
        '<div class="card"><button class="btn btn-primary" style="margin-bottom:14px" onclick="showIndicatorTypeModal()">+ 添加类型</button>'+
        '<table class="table-view"><thead><tr><th>ID</th><th>名称</th><th>描述</th><th>状态</th><th>操作</th></tr></thead><tbody>'+indicatorTypes.map(function(it,i){return '<tr><td style="font-family:monospace">'+it.id+'</td><td><strong>'+esc(it.name)+'</strong></td><td style="font-size:0.82rem;color:var(--muted)">'+esc(it.desc)+'</td><td><label class="toggle-switch"><input type="checkbox" '+(it.active?'checked':'')+' onchange="indicatorTypes['+i+'].active=this.checked;saveIndicatorTypes()"><span class="toggle-slider"></span></label></td><td><button class="btn btn-sm" onclick="showIndicatorTypeModal('+i+')">编辑</button><button class="btn btn-sm btn-danger" onclick="deleteIndicatorType('+i+')">删除</button></td></tr>'}).join('')+'</tbody></table></div>';
}
function showIndicatorTypeModal(idx){var isEdit=typeof idx==='number',it=isEdit?indicatorTypes[idx]:{id:'',name:'',desc:'',active:false};showModal('<h3 style="margin-bottom:12px">'+(isEdit?'编辑':'添加')+'指标类型</h3><label>ID</label><input id="it-id" placeholder="如: budget" value="'+esc(it.id)+'"'+(isEdit?' readonly style="background:var(--bg-soft)"':'')+'><label>名称</label><input id="it-name" placeholder="如: 项目支出绩效评价" value="'+esc(it.name)+'"><label>描述</label><input id="it-desc" placeholder="描述..." value="'+esc(it.desc)+'"><label>状态</label><select id="it-active"><option value="1"'+(it.active?' selected':'')+'>启用</option><option value="0"'+(it.active?'':' selected')+'>禁用</option></select><div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="saveIndicatorType('+(isEdit?idx:-1)+')">保存</button></div>')}
function saveIndicatorType(idx){var data={id:document.getElementById('it-id').value.trim(),name:document.getElementById('it-name').value.trim(),desc:document.getElementById('it-desc').value.trim(),active:document.getElementById('it-active').value==='1'};if(!data.id||!data.name){showToast('请填写ID和名称','error');return}if(idx>=0)indicatorTypes[idx]=data;else indicatorTypes.push(data);saveIndicatorTypes();closeModal();loadIndicatorTypes()}
function deleteIndicatorType(idx){if(!confirm('确定删除？'))return;indicatorTypes.splice(idx,1);saveIndicatorTypes();loadIndicatorTypes()}
function saveIndicatorTypes(){try{localStorage.setItem('admin_indicator_types',JSON.stringify(indicatorTypes));showToast('已保存')}catch(e){showToast('保存失败','error')}}

// ===========================
//  WORKFLOWS / AUDIT / SETTINGS
// ===========================
var wfPage=1,wfStatus='';
async function loadWorkflows(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>⚙️ 工作流监控</h2><p>查看所有用户的工作流执行状态</p></div>'+
        '<div style="display:flex;gap:10px;margin-bottom:14px"><select id="wf-status-filter" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem"><option value="">全部状态</option><option value="created">已创建</option><option value="running">运行中</option><option value="awaiting_approval">待审批</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select></div>'+
        '<div id="wf-table-container" class="card"><div class="empty-state">加载中...</div></div><div id="wf-pagination" class="pagination"></div>';
    document.getElementById('wf-status-filter').addEventListener('change',function(){wfStatus=this.value;wfPage=1;refreshWorkflows()});
    await refreshWorkflows();
}
async function refreshWorkflows(){
    var params=new URLSearchParams({page:wfPage,page_size:20});if(wfStatus)params.set('status',wfStatus);
    var resp=await api(API_BASE+'/workflows?'+params),c=document.getElementById('wf-table-container'),pagDiv=document.getElementById('wf-pagination');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var wfs=resp.data.workflows,total=resp.data.total,page=resp.data.page,ps=resp.data.page_size,tp=Math.ceil(total/ps);
    c.innerHTML='<table class="table-view"><thead><tr><th>Session</th><th>用户</th><th>项目</th><th>状态</th><th>步骤</th><th>Token</th><th>创建</th><th>操作</th></tr></thead><tbody>'+wfs.map(function(w){var sc=w.status==='completed'?'badge-success':w.status==='failed'?'badge-danger':w.status==='running'?'badge-info':'badge-warning';return '<tr><td style="font-family:monospace;font-size:0.75rem">'+esc((w.session_id||'').substring(0,14))+'...</td><td>'+w.user_id+'</td><td>'+esc(w.project_name)+'</td><td><span class="badge '+sc+'">'+esc(w.status)+'</span></td><td>'+w.current_step+'/14</td><td>'+tok(w.token_used)+'</td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(w.created_at)+'</td><td><button class="btn btn-sm" data-act="wf-detail" data-sid="'+esc(w.session_id)+'">详情</button></td></tr>'}).join('')+'</tbody></table>';
    renderPagination(pagDiv,page,tp,function(p){wfPage=p;refreshWorkflows()});
}

// ── 工作流详情 ────────────────────────────────────────
var WF_STATE_META={
    done:{label:'已完成',color:'#22c55e'},
    current:{label:'进行中',color:'var(--accent)'},
    partial:{label:'跑过未完成',color:'#f59e0b'},
    pending:{label:'未开始',color:'var(--border)'}
};

function renderWorkflowTimeline(timeline, sectionMap){
    sectionMap=sectionMap||{};
    var h='<div style="display:grid;grid-template-columns:repeat(14,1fr);gap:6px;margin-bottom:6px">';
    timeline.forEach(function(t){
        var meta=WF_STATE_META[t.state]||WF_STATE_META.pending;
        var sec=sectionMap[t.step];
        var tip='步骤 '+t.step+'：'+meta.label+(t.tokens?'，'+tok(t.tokens)+' token':'')+
                (sec?'（有正文 '+sec.chars+' 字，点击查看）':'（无正文）');
        h+='<div'+(sec?' data-act="wf-step" data-step="'+t.step+'"':'')+' title="'+esc(tip)+'" style="height:34px;border-radius:6px;background:'+meta.color+
           ';opacity:'+(t.state==='pending'?'.45':'1')+';display:flex;align-items:center;justify-content:center;'+
           'color:'+(t.state==='pending'?'var(--text2)':'#fff')+';font-size:.75rem;font-weight:600;'+
           (sec?'cursor:pointer;box-shadow:inset 0 0 0 2px rgba(255,255,255,.85)':'cursor:default')+'">'+t.step+'</div>';
    });
    h+='</div><div style="display:flex;gap:14px;flex-wrap:wrap;font-size:.75rem;color:var(--muted)">';
    ['done','current','partial','pending'].forEach(function(k){
        h+='<span style="display:inline-flex;align-items:center;gap:5px">'+
           '<span style="width:10px;height:10px;border-radius:3px;background:'+WF_STATE_META[k].color+
           ';opacity:'+(k==='pending'?'.45':'1')+'"></span>'+WF_STATE_META[k].label+'</span>';
    });
    h+='</div><div style="font-size:.74rem;color:var(--muted);margin-top:6px">'+
       '带白边的格子可以点开看该步正文</div>';
    return h;
}

function renderIssueList(items,emptyText,color){
    if(!items||!items.length)return '<p style="font-size:.82rem;color:var(--muted)">'+esc(emptyText)+'</p>';
    return items.map(function(it){
        var text=typeof it==='string'?it:(it&&(it.message||it.detail||JSON.stringify(it)))||'';
        var step=it&&it.step?('<span class="badge badge-warning" style="margin-right:6px">步骤 '+it.step+'</span>'):'';
        return '<div style="font-size:.82rem;padding:6px 0;border-top:1px dashed var(--border);color:'+color+'">'+step+esc(text)+'</div>';
    }).join('');
}

function showWorkflowDetail(sessionId){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="empty-state"><p>加载中...</p></div>';
    api(API_BASE+'/workflows/'+encodeURIComponent(sessionId)).then(function(resp){
        if(!resp||!resp.success){main.innerHTML=failBox(resp,'工作流详情加载失败');return}
        main.innerHTML=renderWorkflowDetail(resp.data);
    });
}

var currentWfSession=null;

function renderWorkflowDetail(d){
    currentWfSession=d.session_id;
    var sectionMap={};
    (d.sections||[]).forEach(function(sec){sectionMap[sec.step]=sec});
    var stalled=d.stalled_hours!==null&&d.stalled_hours!==undefined&&d.status!=='completed'&&d.status!=='failed';
    var sc=d.status==='completed'?'badge-success':d.status==='failed'?'badge-danger':'badge-info';
    var h='<div class="page-header" style="display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap">'+
        '<div style="flex:1;min-width:280px"><h2>🔎 '+esc(d.project_name)+'</h2>'+
        '<p style="font-family:monospace;font-size:.76rem">'+esc(d.session_id)+'</p></div>'+
        '<button class="btn" onclick="loadWorkflows()">← 返回列表</button>'+
        (stalled?'<button class="btn btn-danger" data-act="wf-fail" data-sid="'+esc(d.session_id)+'">⚠ 标记为失败</button>':'')+
        '</div>';

    if(stalled){
        h+='<div class="card" style="border-left:4px solid #f59e0b;margin-bottom:16px">'+
           '<strong>已卡住 '+d.stalled_hours+' 小时</strong>'+
           '<span style="font-size:.84rem;color:var(--muted);margin-left:8px">'+
           '未进入终态、且创建已超过一天。确认没人再推进后可以标记为失败。</span></div>';
    }

    var facts=[
        ['状态','<span class="badge '+sc+'">'+esc(d.status)+'</span>'],
        ['用户',esc(d.username||'-')+' (ID:'+d.user_id+')'],
        ['当前步骤',d.current_step+' / '+d.total_steps],
        ['模型',esc(d.model_name||'-')],
        ['RAG',d.use_rag?'开启':'关闭'],
        ['Token 合计',tok(d.token_used)],
        ['创建',formatTime(d.created_at)],
        ['更新',formatTime(d.updated_at)],
        ['完成',formatTime(d.completed_at)]
    ];
    h+='<div class="card" style="margin-bottom:16px"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">'+
       facts.map(function(f){return '<div><div style="font-size:.74rem;color:var(--muted)">'+f[0]+
           '</div><div style="font-size:.88rem;margin-top:2px">'+f[1]+'</div></div>'}).join('')+
       '</div></div>';

    h+='<div class="card" style="margin-bottom:16px"><h3 style="font-size:.95rem;margin-bottom:12px">步骤时间线</h3>'+
       renderWorkflowTimeline(d.timeline,sectionMap)+'</div>';

    h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px">';

    h+='<div class="card"><h3 style="font-size:.95rem;margin-bottom:10px">❌ 错误（'+d.errors.length+'）</h3>'+
       renderIssueList(d.errors,'无记录','var(--danger)')+'</div>';

    h+='<div class="card"><h3 style="font-size:.95rem;margin-bottom:10px">⚠️ 警告（'+d.warnings.length+'）</h3>'+
       renderIssueList(d.warnings,'无记录','var(--warning-fg)')+'</div>';

    h+='<div class="card"><h3 style="font-size:.95rem;margin-bottom:10px">🪙 Token 分布</h3>'+
       (d.tokens_by_step.length
           ? '<table class="table-view"><thead><tr><th>步骤</th><th>Token</th></tr></thead><tbody>'+
             d.tokens_by_step.map(function(t){return '<tr><td>'+(t.step===null?'未关联':('S'+t.step))+'</td><td>'+tok(t.tokens)+'</td></tr>'}).join('')+
             '</tbody></table>'
           : '<p style="font-size:.82rem;color:var(--muted)">没有 Token 记录</p>')+'</div>';

    var a=d.artifacts||{};
    h+='<div class="card"><h3 style="font-size:.95rem;margin-bottom:10px">📦 产出物</h3>'+
       '<div style="font-size:.84rem;line-height:1.9">'+
       '<div>最终报告：'+(a.final_report_chars?('<strong>'+a.final_report_chars+'</strong> 字'):'<span style="color:var(--muted)">尚未生成</span>')+'</div>'+
       '<div>检查点：'+(a.checkpoint_kb?('<strong>'+a.checkpoint_kb+'</strong> KB'):'<span style="color:var(--muted)">无</span>')+'</div>'+
       '<div>状态备份：'+(a.state_backup_kb?('<strong>'+a.state_backup_kb+'</strong> KB'):'<span style="color:var(--muted)">无</span>')+'</div>'+
       '</div>'+
       (a.final_report_chars?'<button class="btn btn-sm btn-primary" style="margin-top:10px" onclick="showWorkflowContent(14)">📄 查看完整报告</button>':'')+
       '</div>';

    h+='</div>';

    if(d.live&&d.live.in_memory){
        h+='<div class="card" style="margin-top:16px;border-left:4px solid var(--accent)">'+
           '<strong>进程内有活实例</strong>：状态 '+esc(d.live.status||'-')+
           '，最后步骤 '+(d.live.last_step||'-')+'</div>';
    }
    return h;
}

async function showWorkflowContent(step){
    if(!currentWfSession){showToast('请先打开一个工作流','error');return}
    showModal(
        '<h3 style="margin-bottom:4px">步骤正文</h3>'+
        '<p id="wfc-sub" style="font-size:.8rem;color:var(--muted);margin-bottom:10px">加载中...</p>'+
        '<div id="wfc-body"><div class="empty-state">加载中...</div></div>'+
        '<div style="margin-top:12px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>'
    );
    var r=await api(API_BASE+'/workflows/'+encodeURIComponent(currentWfSession)+'/content?step='+step);
    var sub=document.getElementById('wfc-sub'), body=document.getElementById('wfc-body');
    if(!body)return;
    if(!r||!r.success){sub.textContent='加载失败';body.innerHTML=failBox(r,'正文加载失败');return}

    var d=r.data;
    var srcName={state_backup:'状态备份',checkpoint_data:'检查点',final_report_column:'数据库',none:'未找到'}[d.source]||d.source;
    sub.textContent='S'+d.step+' · '+d.title+' · 共 '+d.sections.length+' 段 · 来源：'+srcName;

    if(!d.sections.length){
        body.innerHTML='<div class="empty-state"><p>这一步没有正文（可能未执行，或产出为空）</p></div>';
        return;
    }
    var html='';
    d.sections.forEach(function(sec){
        html+='<div style="font-size:.78rem;color:var(--muted);margin:6px 0 4px">'+esc(sec.label)+' · '+sec.chars+' 字</div>'+
              '<pre style="white-space:pre-wrap;word-break:break-word;max-height:46vh;overflow:auto;'+
              'background:var(--bg-soft);border:1px solid var(--border);border-radius:8px;padding:10px;margin:0 0 10px;'+
              'font-family:ui-monospace,Consolas,monospace;font-size:.76rem;line-height:1.55">'+esc(sec.content)+'</pre>';
    });
    if(d.truncated)html+='<p style="font-size:.76rem;color:var(--warning)">内容过长，只显示前 20 万字</p>';
    body.innerHTML=html;
}

async function failWorkflow(sessionId){
    if(!confirm('确认把这个工作流标记为失败吗？\n\n它当前还没结束，标记后不可恢复；进程内实例会一并释放。'))return;
    var r=await api(API_BASE+'/workflows/'+encodeURIComponent(sessionId)+'/fail','POST');
    if(r&&r.success){showToast(r.message);showWorkflowDetail(sessionId)}
    else{showToast((r&&r.detail)||'标记失败','error')}
}

var auditPage=1,auditAction='',auditUserId='';
async function loadAuditLogs(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📋 审计日志</h2><p>追踪所有关键操作记录</p></div>'+
        '<div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap"><select id="audit-action-filter" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem"><option value="">全部操作</option><option value="login">登录</option><option value="logout">登出</option><option value="admin_create_user">创建用户</option><option value="admin_update_user">更新用户</option><option value="admin_delete_user">删除用户</option><option value="workflow_start">启动工作流</option><option value="workflow_complete">完成工作流</option></select>'+
        '<input id="audit-user-filter" placeholder="用户ID" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);width:100px;font-size:0.85rem"><button class="btn btn-sm" onclick="loadAuditSummary()">📊 统计</button></div>'+
        '<div id="audit-table-container" class="card"><div class="empty-state">加载中...</div></div><div id="audit-pagination" class="pagination"></div>';
    document.getElementById('audit-action-filter').addEventListener('change',function(){auditAction=this.value;auditPage=1;refreshAuditLogs()});
    document.getElementById('audit-user-filter').addEventListener('input',debounce(function(){auditUserId=this.value;auditPage=1;refreshAuditLogs()},300));
    await refreshAuditLogs();
}
async function refreshAuditLogs(){
    var params=new URLSearchParams({page:auditPage,page_size:50});if(auditAction)params.set('action',auditAction);if(auditUserId)params.set('user_id',auditUserId);
    var resp=await api(API_BASE+'/audit-logs?'+params),c=document.getElementById('audit-table-container'),pagDiv=document.getElementById('audit-pagination');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var logs=resp.data.logs,total=resp.data.total,page=resp.data.page,ps=resp.data.page_size,tp=Math.ceil(total/ps);
    c.innerHTML='<table class="table-view"><thead><tr><th>ID</th><th>用户</th><th>操作</th><th>资源</th><th>详情</th><th>时间</th></tr></thead><tbody>'+logs.map(function(l){return '<tr><td>'+l.id+'</td><td>'+esc(l.username||'-')+'</td><td><span class="badge badge-info">'+esc(l.action)+'</span></td><td style="font-size:0.8rem">'+esc(l.resource_type||'-')+' / '+esc(l.resource_id||'-')+'</td><td style="font-size:0.82rem">'+esc(l.detail||'-')+'</td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(l.created_at)+'</td></tr>'}).join('')+'</tbody></table>';
    renderPagination(pagDiv,page,tp,function(p){auditPage=p;refreshAuditLogs()});
}
async function loadAuditSummary(){
    var r=await api(API_BASE+'/audit-logs/summary');if(!r||!r.success)return;
    showModal('<h3 style="margin-bottom:12px">📊 操作统计</h3><table class="table-view"><thead><tr><th>操作类型</th><th>次数</th><th>最近时间</th></tr></thead><tbody>'+r.data.map(function(d){return '<tr><td><span class="badge badge-info">'+esc(d.action)+'</span></td><td><strong>'+d.count+'</strong></td><td style="font-size:0.8rem;color:var(--muted)">'+formatTime(d.last_time)+'</td></tr>'}).join('')+'</tbody></table>')
}

async function loadSettings(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="empty-state"><p>加载中...</p></div>';
    var resp=await api(API_BASE+'/settings'),c=document.getElementById('main-content');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var s=resp.data;
    c.innerHTML='<div class="page-header"><h2>⚙️ 系统设置</h2><p>配置系统全局参数</p></div>'+
        '<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:14px">🤖 AI聊天功能</h3>'+
        '<div style="display:flex;align-items:center;gap:16px"><label class="toggle-switch"><input type="checkbox" id="ai-chat-toggle" '+(s.ai_chat_enabled?'checked':'')+' onchange="toggleAIChat(this.checked)"><span class="toggle-slider"></span></label>'+
        '<div><div style="font-weight:600">'+(s.ai_chat_enabled?'已启用':'已禁用')+'</div><div style="font-size:0.8rem;color:var(--muted)">客户端AI助手功能</div></div></div></div>'+
        '<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:14px">🛟 备用模型（降级链）</h3>'+
        '<p style="font-size:0.85rem;color:var(--muted);margin-bottom:10px">主模型遇到 429/超时/5xx 时，先重试一次，仍失败则按顺序换用下面的模型。留空表示不降级。最多 2 个。</p>'+
        '<div style="display:flex;gap:10px;align-items:flex-end;max-width:560px">'+
        '<div style="flex:1"><label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">模型名（英文逗号分隔，按优先级）</label>'+
        '<input id="fallbackModels" class="input-field" placeholder="例如 deepseek-chat,qwen-plus" value="'+esc((s.fallback_models||[]).join(','))+'"></div>'+
        '<button class="btn btn-primary" onclick="saveFallbackModels()">💾 保存</button></div>'+
        '<div style="font-size:0.78rem;color:var(--muted);margin-top:8px">提示：降级会改变报告措辞，导出件会自动注明哪一步换了模型。</div></div>'+
        '<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:14px">🔒 修改管理员密码</h3>'+
        '<div style="display:flex;flex-direction:column;gap:12px;max-width:360px">'+
        '<div><label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">旧密码</label><input type="password" id="adminOldPassword" class="input-field" placeholder="输入旧密码"></div>'+
        '<div><label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">新密码</label><input type="password" id="adminNewPassword" class="input-field" placeholder="输入新密码（至少6位）"></div>'+
        '<div><label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">确认新密码</label><input type="password" id="adminConfirmPassword" class="input-field" placeholder="再次输入新密码"></div>'+
        '<div id="adminPwdError" style="color:var(--danger);font-size:0.82rem;display:none"></div>'+
        '<button class="btn btn-primary" id="adminPwdBtn" onclick="changeAdminPassword()">修改密码</button>'+
        '<p id="adminPwdSuccess" style="color:var(--success);font-size:0.82rem;margin-top:8px;display:none">密码修改成功！</p></div></div>'+
        '<div class="card"><h3 style="margin-bottom:14px">📏 评分等级阈值</h3><p style="font-size:0.85rem;color:var(--muted);margin-bottom:14px">绩效评价自动等级判定标准</p>'+
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px"><div><label>优秀 ≥</label><input id="rt-excellent" type="number" min="0" max="100" step="0.1" value="'+(s.rating_thresholds.excellent||90)+'"></div><div><label>良好 ≥</label><input id="rt-good" type="number" min="0" max="100" step="0.1" value="'+(s.rating_thresholds.good||75)+'"></div><div><label>合格 ≥</label><input id="rt-pass" type="number" min="0" max="100" step="0.1" value="'+(s.rating_thresholds.pass||60)+'"></div></div>'+
        '<button class="btn btn-primary" onclick="saveRatingThresholds()">💾 保存阈值</button></div>';
}
async function saveFallbackModels(){
    var raw=(document.getElementById('fallbackModels').value||'').split(',');
    var models=[];for(var i=0;i<raw.length;i++){var v=raw[i].trim();if(v&&models.indexOf(v)<0)models.push(v)}
    if(models.length>2){showToast('最多 2 个备选模型','error');return}
    var r=await api(API_BASE+'/settings/fallback-models','POST',{models:models});
    if(r&&r.success){showToast(r.message);loadSettings()}else{showToast(r&&r.detail||'保存失败','error')}
}

async function toggleAIChat(e){var r=await api(API_BASE+'/settings/ai-chat-toggle','POST',{ai_chat_enabled:e});if(r&&r.success)showToast(r.message);else showToast((r&&r.detail)||'操作失败','error')}
async function saveRatingThresholds(){
    var e=parseFloat(document.getElementById('rt-excellent').value)||90,g=parseFloat(document.getElementById('rt-good').value)||75,p=parseFloat(document.getElementById('rt-pass').value)||60;
    if(e<=g||g<=p){showToast('优秀 > 良好 > 合格','error');return}
    var r=await api(API_BASE+'/settings/rating-thresholds','POST',{excellent:e,good:g,pass_:p});
    if(r&&r.success){showToast(r.message);loadSettings()}else{showToast(r&&r.detail||'保存失败','error')}
}
async function changeAdminPassword(){
    var o=document.getElementById('adminOldPassword').value.trim(),n=document.getElementById('adminNewPassword').value,cn=document.getElementById('adminConfirmPassword').value,err=document.getElementById('adminPwdError'),ok=document.getElementById('adminPwdSuccess'),btn=document.getElementById('adminPwdBtn');
    err.style.display='none';ok.style.display='none';
    if(!o||!n||!cn){err.textContent='请填写所有密码字段';err.style.display='block';return}
    if(n.length<6){err.textContent='新密码长度不能少于6位';err.style.display='block';return}
    if(n!==cn){err.textContent='两次输入的新密码不一致';err.style.display='block';return}
    btn.disabled=true;btn.textContent='修改中...';
    var r=await api(API_BASE+'/me/password','PUT',{old_password:o,new_password:n});
    if(r&&r.success){
        // 同理：旧令牌随 token_version 作废，存下新令牌才能继续用这个页面
        if(r.data&&r.data.access_token){accessToken=r.data.access_token;localStorage.setItem('access_token',accessToken);localStorage.setItem('token',accessToken)}
        ok.style.display='block';document.getElementById('adminOldPassword').value='';document.getElementById('adminNewPassword').value='';document.getElementById('adminConfirmPassword').value='';showToast('密码修改成功')}
    else{err.textContent=r&&r.detail||'修改失败';err.style.display='block'}
    btn.disabled=false;btn.textContent='修改密码';
}

// ===========================
//  NOTIFICATIONS
// ===========================
var notifPage=1,notifAllUsers=[];
async function renderNotifications(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>🔔 通知管理</h2><p>向客户端用户发送系统通知</p></div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">'+
        '<div class="card"><h3 style="margin-bottom:14px">✉️ 发送通知</h3>'+
        '<label>通知标题</label><input id="notif-title" class="input-field" placeholder="请输入通知标题" style="margin-bottom:10px">'+
        '<label>通知内容</label><textarea id="notif-content" style="width:100%;min-height:120px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;font-size:.88rem;color:var(--text);resize:vertical;margin-bottom:10px;font-family:inherit" placeholder="请输入通知内容..."></textarea>'+
        '<label>发送对象</label>'+
        '<div style="margin-bottom:10px"><label style="display:flex;align-items:center;gap:8px;font-size:.88rem;cursor:pointer"><input type="radio" name="notif-target" value="all" checked onchange="toggleNotifTarget()"> 全部用户</label>'+
        '<label style="display:flex;align-items:center;gap:8px;font-size:.88rem;cursor:pointer;margin-top:4px"><input type="radio" name="notif-target" value="specific" onchange="toggleNotifTarget()"> 指定用户</label></div>'+
        '<div id="notif-user-select" style="display:none;margin-bottom:10px"><select id="notif-users" multiple style="width:100%;height:120px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px;font-size:.85rem;color:var(--text)"></select><p style="font-size:.75rem;color:var(--muted);margin-top:4px">按住 Ctrl 可多选</p></div>'+
        '<button class="btn btn-primary" onclick="sendNotification()">📨 发送通知</button></div>'+
        '<div class="card"><h3 style="margin-bottom:14px">📋 发送记录</h3><div id="notif-history"><div class="empty-state">加载中...</div></div>'+
        '<div id="notif-pagination" class="pagination"></div></div></div>';
    // 加载用户列表（走轻量选项接口：/users 的 page_size 上限是 100，传 9999 会 422）
    try{
        var ur=await api(API_BASE+'/users/options?role=user');
        if(ur&&ur.success){
            notifAllUsers=ur.data.users||[];
            var sel=document.getElementById('notif-users');
            if(sel){
                sel.innerHTML=notifAllUsers.map(function(u){return '<option value="'+u.id+'">'+esc(u.username)+' (ID:'+u.id+')</option>'}).join('');
                if(!notifAllUsers.length)sel.innerHTML='<option value="" disabled>暂无普通用户</option>';
            }
        }else{
            showToast((ur&&ur.detail)||'用户列表加载失败','error');
        }
    }catch(e){showToast('用户列表加载失败: '+(e.message||'网络错误'),'error')}
    refreshNotifHistory();
}
function toggleNotifTarget(){
    var sel=document.getElementById('notif-user-select');
    var radio=document.querySelector('input[name="notif-target"]:checked');
    sel.style.display=radio&&radio.value==='specific'?'block':'none';
}
async function sendNotification(){
    var title=document.getElementById('notif-title').value.trim();
    var content=document.getElementById('notif-content').value.trim();
    if(!title){showToast('请输入通知标题','error');return}
    if(!content){showToast('请输入通知内容','error');return}
    var target=document.querySelector('input[name="notif-target"]:checked').value;
    var body={title:title,content:content};
    if(target==='specific'){
        var sel=document.getElementById('notif-users');
        var ids=[];for(var i=0;i<sel.options.length;i++){if(sel.options[i].selected)ids.push(parseInt(sel.options[i].value))}
        if(!ids.length){showToast('请选择至少一个用户','error');return}
        body.user_ids=ids;
    }
    var sendBtn=document.querySelector('[onclick="sendNotification()"]');
    if(sendBtn){sendBtn.disabled=true;sendBtn.dataset.origText=sendBtn.textContent;sendBtn.textContent='发送中...'}
    var resp=await api(API_BASE+'/notifications','POST',body);
    if(sendBtn){sendBtn.disabled=false;if(sendBtn.dataset.origText)sendBtn.textContent=sendBtn.dataset.origText}
    if(resp&&resp.success){showToast(resp.message);document.getElementById('notif-title').value='';document.getElementById('notif-content').value='';notifPage=1;refreshNotifHistory()}else{showToast(resp&&resp.detail||'发送失败','error')}
}
async function refreshNotifHistory(){
    var resp=await api(API_BASE+'/notifications?page='+notifPage+'&page_size=15');
    var c=document.getElementById('notif-history'),pagDiv=document.getElementById('notif-pagination');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var items=resp.data.notifications,total=resp.data.total,page=resp.data.page,ps=resp.data.page_size,tp=Math.ceil(total/ps);
    if(!items.length){c.innerHTML='<div class="empty-state"><p>暂无发送记录</p></div>';pagDiv.innerHTML='';return}
    c.innerHTML='<table class="table-view"><thead><tr><th>ID</th><th>接收用户</th><th>标题</th><th>内容</th><th>状态</th><th>发送时间</th><th>操作</th></tr></thead><tbody>'+items.map(function(n){return '<tr><td>'+n.id+'</td><td>'+esc(n.username)+' (ID:'+n.user_id+')</td><td>'+esc(n.title)+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(n.content)+'</td><td><span class="badge '+(n.is_read?'badge-success':'badge-warning')+'">'+(n.is_read?'已读':'未读')+'</span></td><td style="font-size:0.78rem;color:var(--muted)">'+formatTime(n.created_at)+'</td><td><button class="btn btn-sm" style="background:var(--danger-bg);color:var(--danger-fg);padding:4px 10px;font-size:.78rem;border:none;border-radius:6px;cursor:pointer" onclick="withdrawNotification('+n.id+')">撤回</button></td></tr>'}).join('')+'</tbody></table>';
    renderPagination(pagDiv,page,tp,function(p){notifPage=p;refreshNotifHistory()});
}
async function withdrawNotification(id){
    if(!confirm('确认撤回此通知？撤回后所有客户端将不可见。'))return;
    var resp=await api(API_BASE+'/notifications/'+id,'DELETE');
    if(resp&&resp.success){showToast('通知已撤回');refreshNotifHistory()}else{showToast(resp&&resp.detail||'撤回失败','error')}
}

// ═══════════════════════════════════════════════
//  模块排序管理（步骤6/13/14）
// ═══════════════════════════════════════════════
var moduleSeqStep='', moduleSeqItems=[];
async function loadModuleSequences(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📋 模块排序管理</h2><p>配置步骤6/13/14中报告模块的显示顺序与名称（管理端独有配置，客户端不可修改名称）</p></div>'+
        '<div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap">'+
        '<select id="ms-step-filter" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem;min-width:180px">'+
        '<option value="">全部步骤</option>'+
        [6,13,14].map(function(i){return '<option value="'+i+'">步骤 '+i+'</option>'}).join('')+'</select>'+
        '<button class="btn btn-primary" onclick="showModuleSeqModal()">+ 添加模块</button>'+
        '<button class="btn btn-sm" onclick="saveAllModuleSeq()">💾 批量保存排序</button></div>'+
        '<div class="card"><div id="ms-container"><div class="empty-state">加载中...</div></div></div>';
    document.getElementById('ms-step-filter').addEventListener('change',function(){moduleSeqStep=this.value;refreshModuleSequences()});
    await refreshModuleSequences();
}
async function refreshModuleSequences(){
    var params=moduleSeqStep?'?step_number='+moduleSeqStep:'',c=document.getElementById('ms-container');
    var resp=await api(API_BASE+'/module-sequences'+params);
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    moduleSeqItems=resp.data;
    if(!moduleSeqItems.length){c.innerHTML='<div class="empty-state"><p>暂无模块顺序配置</p><p style="font-size:0.82rem;color:var(--muted)">点击"添加模块"开始配置</p></div>';return}
    // 按步骤分组
    var groups={};
    moduleSeqItems.forEach(function(m){
        var k=''+m.step_number;if(!groups[k])groups[k]=[];groups[k].push(m);
    });
    Object.keys(groups).forEach(function(k){groups[k].sort(function(a,b){return a.sort_order-b.sort_order})});
    var h='';
    Object.keys(groups).sort().forEach(function(step){
        h+='<h4 style="margin:16px 0 8px;color:var(--accent)">📌 步骤 '+step+'</h4>';
        groups[step].forEach(function(m,i){
            h+='<div style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--card);border:1px solid var(--border);border-radius:7px;margin-bottom:4px">'+
                '<span style="color:var(--muted);font-size:0.78rem;min-width:20px">'+(i+1)+'</span>'+
                '<span style="flex:1;font-size:0.9rem">'+esc(m.module_name)+'</span>'+
                '<span style="font-size:0.75rem;color:var(--muted)">key: '+esc(m.module_key)+'</span>'+
                '<label class="toggle-switch" title="客户端可见"><input type="checkbox" '+(m.is_visible?'checked':'')+' onchange="toggleModuleSeqVisible('+m.id+',this.checked)"><span class="toggle-slider"></span></label>'+
                '<label class="toggle-switch" title="客户端可调序"><input type="checkbox" '+(m.client_editable?'checked':'')+' onchange="toggleModuleSeqEditable('+m.id+',this.checked)"><span class="toggle-slider"></span></label>'+
                '<button class="btn btn-sm" onclick="moveModuleSeq('+m.id+',-1)">▲</button>'+
                '<button class="btn btn-sm" onclick="moveModuleSeq('+m.id+',1)">▼</button>'+
                '<button class="btn btn-sm" onclick="showModuleSeqModal('+m.id+')">编辑</button>'+
                '<button class="btn btn-sm btn-danger" onclick="deleteModuleSeq('+m.id+')">删除</button></div>';
        });
    });
    c.innerHTML=h+'<div style="margin-top:12px;font-size:0.78rem;color:var(--muted)">💡 提示：🔘左开关=客户端可见性 | 🔘右开关=客户端是否可调序（不可改名）</div>';
}
function showModuleSeqModal(id){
    var isEdit=!!id,item=isEdit?moduleSeqItems.find(function(m){return m.id===id}):null;
    showModal('<h3 style="margin-bottom:12px">'+(isEdit?'编辑':'添加')+'模块</h3>'+
        '<label>步骤编号</label><select id="ms-step"><option value="6">步骤6 - 指标体系表</option><option value="13">步骤13 - 工作开展情况</option><option value="14">步骤14 - 评价报告</option></select>'+
        '<label>模块Key（唯一标识）</label><input id="ms-key" placeholder="如: indicator_table" value="'+esc(item?item.module_key:'')+'">'+
        '<label>模块名称</label><input id="ms-name" placeholder="如: 指标体系分析表" value="'+esc(item?item.module_name:'')+'">'+
        '<div class="form-row"><div><label>排序</label><input id="ms-order" type="number" min="0" value="'+(item?item.sort_order:0)+'"></div></div>'+
        '<div class="form-row"><div><label>客户端可见</label><select id="ms-visible"><option value="1"'+(item&&item.is_visible?' selected':'')+'>是</option><option value="0"'+(item&&!item.is_visible?' selected':'')+'>否</option></select></div><div><label>客户端可调序</label><select id="ms-editable"><option value="1"'+(item&&item.client_editable?' selected':'')+'>是</option><option value="0"'+(item&&!item.client_editable?' selected':'')+'>否</option></select></div></div>'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="saveModuleSeq('+(id||0)+')">保存</button></div>');
    document.getElementById('ms-step').value=item?item.step_number:6;
}
async function saveModuleSeq(id){
    var data={step_number:parseInt(document.getElementById('ms-step').value),module_key:document.getElementById('ms-key').value.trim(),module_name:document.getElementById('ms-name').value.trim(),sort_order:parseInt(document.getElementById('ms-order').value)||0,is_visible:document.getElementById('ms-visible').value==='1',client_editable:document.getElementById('ms-editable').value==='1'};
    if(!data.module_key||!data.module_name){showToast('请填写Key和名称','error');return}
    var resp=await api(API_BASE+'/module-sequences','POST',data);
    if(resp&&resp.success){showToast(resp.message);closeModal();refreshModuleSequences()}else{showToast(resp&&resp.detail||'保存失败','error')}
}
async function moveModuleSeq(id,dir){
    var idx=-1;for(var i=0;i<moduleSeqItems.length;i++){if(moduleSeqItems[i].id===id){idx=i;break}}
    if(idx<0)return;var ni=idx+dir;if(ni<0||ni>=moduleSeqItems.length)return;
    var tmp=moduleSeqItems[idx].sort_order;moduleSeqItems[idx].sort_order=moduleSeqItems[ni].sort_order;moduleSeqItems[ni].sort_order=tmp;
    await saveAllModuleSeq();
}
async function saveAllModuleSeq(){
    var modules=moduleSeqItems.map(function(m){return{step_number:m.step_number,module_key:m.module_key,module_name:m.module_name,sort_order:m.sort_order,is_visible:m.is_visible,client_editable:m.client_editable}});
    var resp=await api(API_BASE+'/module-sequences/batch','PUT',{modules:modules});
    if(resp&&resp.success){showToast(resp.message);refreshModuleSequences()}else{showToast(resp&&resp.detail||'保存失败','error')}
}
async function toggleModuleSeqVisible(id,v){var m=moduleSeqItems.find(function(x){return x.id===id});if(m)m.is_visible=v;await saveAllModuleSeq()}
async function toggleModuleSeqEditable(id,v){var m=moduleSeqItems.find(function(x){return x.id===id});if(m)m.client_editable=v;await saveAllModuleSeq()}
async function deleteModuleSeq(id){if(!confirm('确定删除？'))return;var r=await api(API_BASE+'/module-sequences/'+id,'DELETE');if(r&&r.success){showToast('已删除');refreshModuleSequences()}else{showToast(r&&r.detail||'删除失败','error')}}


// ═══════════════════════════════════════════════
//  步骤配置概览
// ═══════════════════════════════════════════════
var stepConfigData=[];
async function loadStepConfig(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>🔍 步骤配置概览</h2><p>14步工作流每步的Prompt + 知识库 + 模块序列配置情况总览</p></div>'+
        '<div class="card"><div id="sc-container"><div class="empty-state">加载中...</div></div></div>';
    var resp=await api(API_BASE+'/step-config');
    if(!resp||!resp.success){document.getElementById('sc-container').innerHTML=failBox(resp,'步骤配置加载失败');return}
    stepConfigData=resp.data;
    var stepNames=['资料清单','核心数据提取','指标体系构建','分值设定','评分标准','指标体系表生成','指标分析','经验做法','问题及原因分析','改进建议','综合评价','基本情况','工作开展情况','评价报告'];
    var h='<table class="table-view"><thead><tr><th>步骤</th><th>名称</th><th>Prompt数</th><th>知识库条目</th><th>模块序列</th><th>状态</th></tr></thead><tbody>';
    stepConfigData.forEach(function(s,i){
        var total=s.prompt_count+s.kb_count+s.module_count,statusIcon=total>0?'✅':'⚠️',statusColor=total>0?'var(--success)':'var(--warning)';
        h+='<tr>'+
            '<td style="font-family:monospace;font-weight:700">S'+(i+1)+'</td>'+
            '<td>'+esc(stepNames[i]||'步骤'+(i+1))+'</td>'+
            '<td><span class="badge '+(s.prompt_count>0?'badge-info':'badge-warning')+'">'+s.prompt_count+'个</span></td>'+
            '<td><span class="badge '+(s.kb_count>0?'badge-info':'badge-warning')+'">'+s.kb_count+'个</span></td>'+
            '<td><span class="badge '+(s.module_count>0?'badge-info':'')+'">'+s.module_count+'个</span></td>'+
            '<td style="color:'+statusColor+'">'+statusIcon+' '+(total>0?'已配置':'待配置')+'</td>'+
            '</tr>';
    });
    h+='</tbody></table>';
    document.getElementById('sc-container').innerHTML=h;
}


// ═══════════════════════════════════════════════
//  提示词管理（增强：支持指标层级/维度）
// ═══════════════════════════════════════════════
var P_LEVELS=['','一级','二级','三级'];

async function loadPrompts(){
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>📝 提示词管理</h2><p>编辑各工作流步骤的Prompt模板（支持按指标层级/维度独立配置）</p></div>'+
        '<div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap">'+
        '<label style="font-size:0.85rem;color:var(--muted);white-space:nowrap">步骤：</label>'+
        '<select id="prompts-filter-s" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem;min-width:140px">'+
        '<option value="">全部步骤</option>'+[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14].map(function(i){return '<option value="'+i+'">步骤 '+i+'</option>'}).join('')+'</select>'+
        '<label style="font-size:0.85rem;color:var(--muted);white-space:nowrap">层级：</label>'+
        '<select id="prompts-filter-l" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem;min-width:100px">'+
        '<option value="">全部</option><option value="1">一级指标</option><option value="2">二级指标</option><option value="3">三级指标</option></select>'+
        '<label style="font-size:0.85rem;color:var(--muted);white-space:nowrap">维度：</label>'+
        '<select id="prompts-filter-d" style="padding:7px 11px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.85rem;min-width:110px">'+
        '<option value="">全部</option><option value="决策">决策</option><option value="过程">过程</option><option value="产出">产出</option><option value="效益">效益</option></select>'+
        '<button class="btn btn-sm" onclick="toggleAllAccordions()">展开/折叠全部</button>'+
        '<button class="btn btn-primary btn-sm" onclick="showGranularPromptModal()">+ 添加提示词</button></div>'+
        '<div id="prompts-container"><div class="empty-state">加载中...</div></div>';
    var filterS=document.getElementById('prompts-filter-s');
    var filterL=document.getElementById('prompts-filter-l');
    var filterD=document.getElementById('prompts-filter-d');
    var applyFilter=function(){promptsFilterStep=filterS.value;promptsFilterLevel=filterL.value;promptsFilterDim=filterD.value;refreshPrompts()};
    filterS.addEventListener('change',applyFilter);filterL.addEventListener('change',applyFilter);filterD.addEventListener('change',applyFilter);
    await refreshPrompts();
}
var promptsFilterStep='',promptsFilterLevel='',promptsFilterDim='';
async function refreshPrompts(){
    var params=new URLSearchParams();if(promptsFilterStep!=='')params.set('step_number',promptsFilterStep);if(promptsFilterLevel!=='')params.set('indicator_level',promptsFilterLevel);if(promptsFilterDim!=='')params.set('dimension',promptsFilterDim);
    var resp=await api(API_BASE+'/prompts?'+params),c=document.getElementById('prompts-container');
    if(!resp||!resp.success){c.innerHTML=failBox(resp,'加载失败');return}
    var prompts=resp.data;
    if(!prompts.length){c.innerHTML='<div class="empty-state"><p>暂无提示词数据</p></div>';return}
    var h='';prompts.forEach(function(p){
        // 粒度标签
        var tags='';if(p.indicator_level)tags+='<span class="badge badge-info">'+P_LEVELS[p.indicator_level]+'指标</span>';if(p.parent_indicator)tags+='<span class="badge" style="background:var(--info-bg);color:var(--info-fg)">'+esc(p.parent_indicator)+'</span>';if(p.dimension)tags+='<span class="badge" style="background:#fce7f3;color:#be185d">'+esc(p.dimension)+'</span>';
        h+='<div class="step-accordion"><div class="step-accordion-header" onclick="toggleAccordion(this)"><span style="font-weight:700;color:var(--accent);min-width:30px">S'+p.step_number+'</span><span style="flex:1">'+esc(p.step_name)+'</span>'+tags+'<span class="badge badge-info">v'+p.version+'</span><span style="font-size:0.75rem;color:var(--muted)">'+formatTime(p.updated_at)+'</span></div>'+
        '<div class="step-accordion-body">'+
        (!p.is_system?'<div style="margin-bottom:8px;font-size:0.8rem;color:var(--muted)">层级: '+(P_LEVELS[p.indicator_level||0]||'全部')+' | 维度: '+(p.dimension||'全部')+' | 上级指标: '+(p.parent_indicator||'无')+'</div>':'')+
        '<textarea id="prompt-'+p.id+'" style="width:100%;min-height:180px;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);padding:10px;font-size:0.85rem;font-family:monospace;resize:vertical">'+esc(p.prompt_content||'')+'</textarea>'+
        '<div style="margin-top:8px;display:flex;gap:8px;align-items:center"><input id="changelog-'+p.id+'" placeholder="变更说明..." style="flex:1;min-width:150px;padding:6px 10px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:0.82rem">'+
        '<button class="btn btn-sm" onclick="showPromptHistory('+p.id+')">🕘 历史</button>'+
        '<button class="btn btn-primary btn-sm" onclick="saveGranularPrompt('+p.id+')">💾 保存</button>'+
        (!p.is_system?'<button class="btn btn-sm btn-danger" onclick="deleteGranularPrompt('+p.id+')">🗑 删除</button>':'')+
        '<button class="btn btn-sm" onclick="resetPrompt('+p.id+')">↺ 重置</button></div>'+
        (p.change_log?'<div style="margin-top:6px;font-size:0.75rem;color:var(--muted)">📋 '+esc(p.change_log)+' (v'+p.version+')</div>':'')+
        '</div></div>'
    });c.innerHTML=h
}
async function saveGranularPrompt(id){
    var p=document.getElementById('prompt-'+id).value,l=document.getElementById('changelog-'+id).value.trim();
    var saveBtn=document.querySelector('[onclick="saveGranularPrompt('+id+')"]');
    if(saveBtn){saveBtn.disabled=true;saveBtn.dataset.origText=saveBtn.textContent;saveBtn.textContent='保存中...'}
    var r=await api(API_BASE+'/prompts/'+id,'PUT',{prompt_content:p,change_log:l||'手动更新'});
    if(saveBtn){saveBtn.disabled=false;if(saveBtn.dataset.origText)saveBtn.textContent=saveBtn.dataset.origText}
    if(r&&r.success){showToast(r.message);refreshPrompts()}else{showToast(r&&r.detail||'保存失败','error')}
}
async function deleteGranularPrompt(id){if(!confirm('确定删除此提示词？'))return;var r=await api(API_BASE+'/prompts/'+id,'DELETE');if(r&&r.success){showToast('已删除');refreshPrompts()}else{showToast(r&&r.detail||'删除失败','error')}}
function toggleAccordion(header){var body=header.nextElementSibling;if(!body)return;if(body.style.display==='block'){body.style.display='none'}else{body.style.display='block'}}
function toggleAllAccordions(){var bodies=document.querySelectorAll('.step-accordion-body');if(!bodies.length)return;var allOpen=[].every.call(bodies,function(b){return b.style.display==='block'});for(var i=0;i<bodies.length;i++){bodies[i].style.display=allOpen?'none':'block'}}
async function resetPrompt(id){
    if(!confirm('确定重置此提示词为系统默认？'))return;
    var r=await api(API_BASE+'/prompts/'+id+'/reset','POST');
    if(r&&r.success){showToast(r.message);refreshPrompts()}else{showToast(r&&r.detail||'重置失败','error')}
}

// ══════════════════════════════════════════════════════
//  提示词版本历史（对比 + 回滚）
// ══════════════════════════════════════════════════════
async function showPromptHistory(promptId){
    var resp=await api(API_BASE+'/prompts/'+promptId+'/versions');
    if(!resp||!resp.success){showToast((resp&&resp.detail)||'版本历史加载失败','error');return}
    var d=resp.data,p=d.prompt,rows=d.versions||[];

    var body=rows.length
        ? '<table class="table-view"><thead><tr><th>版本</th><th>说明</th><th>操作人</th><th>长度</th><th>时间</th><th>操作</th></tr></thead><tbody>'+
          rows.map(function(v){
              return '<tr><td><strong>v'+v.version+'</strong>'+(v.is_current?' <span class="badge badge-success">当前</span>':'')+'</td>'+
                  '<td style="font-size:.82rem">'+esc(v.change_log||'-')+'</td>'+
                  '<td style="font-size:.82rem">'+esc(v.modified_by_name||'-')+'</td>'+
                  '<td style="font-size:.78rem;color:var(--muted)">'+v.length+' 字</td>'+
                  '<td style="font-size:.78rem;color:var(--muted)">'+formatTime(v.created_at)+'</td>'+
                  '<td style="white-space:nowrap"><button class="btn btn-sm" onclick="viewPromptVersion('+promptId+','+v.version+')">对比</button>'+
                  (v.is_current?'':'<button class="btn btn-sm btn-primary" onclick="rollbackPrompt('+promptId+','+v.version+')">回滚</button>')+
                  '</td></tr>';
          }).join('')+'</tbody></table>'
        : '<div class="empty-state"><p>还没有历史版本</p><p style="font-size:.8rem;color:var(--muted)">下次保存时会先把当前内容存成快照</p></div>';

    showModal(
        '<h3 style="margin-bottom:6px">🕘 版本历史</h3>'+
        '<p style="font-size:.82rem;color:var(--muted);margin-bottom:12px">S'+p.step_number+' · '+esc(p.step_name)+'　当前 v'+p.current_version+'</p>'+
        body+
        '<div style="margin-top:14px;text-align:right"><button class="btn" onclick="closeModal()">关闭</button></div>'
    );
}

// 行级对比：掐掉公共前后缀，中间那段标成删除/新增。
// 不做完整 LCS —— 提示词经常上千行，O(n²) 会把页面卡住，而实际改动基本都是局部的。
function renderPromptDiff(oldText,newText){
    var a=String(oldText||'').split('\n'),b=String(newText||'').split('\n');
    var head=0;
    while(head<a.length&&head<b.length&&a[head]===b[head])head++;
    var ta=a.length-1,tb=b.length-1;
    while(ta>=head&&tb>=head&&a[ta]===b[tb]){ta--;tb--}

    function block(lines,mark,style){
        var LIMIT=120,out='';
        lines.slice(0,LIMIT).forEach(function(l){
            out+='<div style="'+style+'"><span style="opacity:.55;user-select:none">'+mark+' </span>'+esc(l||' ')+'</div>';
        });
        if(lines.length>LIMIT){
            out+='<div style="color:var(--muted);padding:2px 0">… 省略 '+(lines.length-LIMIT)+' 行</div>';
        }
        return out;
    }

    var html='';
    if(head>0)html+=block(a.slice(0,head),' ','color:var(--muted)');
    var removed=a.slice(head,ta+1),added=b.slice(head,tb+1);
    html+=block(removed,'−','background:var(--danger-bg);color:var(--danger-fg)');
    html+=block(added,'+','background:var(--success-bg);color:var(--success-fg)');
    if(tb<b.length-1)html+=block(b.slice(tb+1),' ','color:var(--muted)');
    if(!removed.length&&!added.length)html='<div style="color:var(--muted)">这两版内容一致</div>';
    return html;
}

async function viewPromptVersion(promptId,version){
    var r=await api(API_BASE+'/prompts/'+promptId+'/versions/'+version);
    if(!r||!r.success){showToast((r&&r.detail)||'版本内容加载失败','error');return}
    var d=r.data;
    showModal(
        '<h3 style="margin-bottom:6px">v'+version+' ↔ 当前 v'+d.current_version+'</h3>'+
        '<p style="font-size:.8rem;color:var(--muted);margin-bottom:10px">红色是 v'+version+' 里有、当前没有的；绿色是当前新增的</p>'+
        '<div style="max-height:52vh;overflow:auto;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px;font-family:monospace;font-size:.76rem;line-height:1.55;white-space:pre-wrap;word-break:break-word">'+
        renderPromptDiff(d.prompt_content,d.current_content)+'</div>'+
        '<div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">'+
        '<button class="btn" onclick="showPromptHistory('+promptId+')">返回列表</button>'+
        '<button class="btn btn-primary" onclick="rollbackPrompt('+promptId+','+version+')">回滚到 v'+version+'</button></div>'
    );
}

async function rollbackPrompt(promptId,version){
    if(!confirm('回滚到 v'+version+' 吗？\n\n当前内容会先存进历史，回滚本身也记为新版本 —— 不会丢东西。'))return;
    var r=await api(API_BASE+'/prompts/'+promptId+'/rollback/'+version,'POST');
    if(r&&r.success){showToast(r.message);closeModal();refreshPrompts()}
    else{showToast((r&&r.detail)||'回滚失败','error')}
}
function showGranularPromptModal(){
    showModal('<h3 style="margin-bottom:12px">添加细粒度提示词</h3>'+
        '<div class="form-row"><div><label>步骤编号</label><select id="gp-step">'+[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14].map(function(i){return '<option value="'+i+'">步骤 '+i+'</option>'}).join('')+'</select></div><div><label>指标层级（可选）</label><select id="gp-level"><option value="">全部级别</option><option value="1">一级指标</option><option value="2">二级指标</option><option value="3">三级指标</option></select></div></div>'+
        '<label>步骤名称</label><input id="gp-name" placeholder="如: 指标体系构建-一级指标">'+
        '<div class="form-row"><div><label>上级指标（可选）</label><input id="gp-parent" placeholder="如: 决策类"></div><div><label>维度（步骤11用）</label><select id="gp-dim"><option value="">无</option><option value="决策">决策</option><option value="过程">过程</option><option value="产出">产出</option><option value="效益">效益</option></select></div></div>'+
        '<label>Prompt内容</label><textarea id="gp-content" placeholder="输入提示词内容..." style="min-height:120px"></textarea>'+
        '<label>变更说明</label><input id="gp-clog" placeholder="初始创建">'+
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn btn-primary" onclick="saveNewGranularPrompt()">保存</button></div>');
}
async function saveNewGranularPrompt(){
    var data={step_number:parseInt(document.getElementById('gp-step').value),step_name:document.getElementById('gp-name').value.trim(),prompt_content:document.getElementById('gp-content').value.trim(),indicator_level:document.getElementById('gp-level').value?parseInt(document.getElementById('gp-level').value):null,parent_indicator:document.getElementById('gp-parent').value.trim()||null,dimension:document.getElementById('gp-dim').value||null,change_log:document.getElementById('gp-clog').value.trim()||'初始创建'};
    if(!data.step_name||!data.prompt_content){showToast('请填写名称和内容','error');return}
    var resp=await api(API_BASE+'/prompts/granular','POST',data);
    if(resp&&resp.success){showToast(resp.message);closeModal();refreshPrompts()}else{showToast(resp&&resp.detail||'创建失败','error')}
}

// ═══════════════════════════════════════
// ══════════════════════════════════════════════════════
//  系统健康
// ══════════════════════════════════════════════════════
var healthTimer=null;

function healthMeta(level){
    if(level==='ok')return{color:'var(--success-strong)',dot:'#22c55e',text:'正常'};
    if(level==='warn')return{color:'var(--warning-fg)',dot:'#f59e0b',text:'注意'};
    return{color:'var(--danger)',dot:'#ef4444',text:'异常'};
}

function healthDetailRows(detail){
    if(!detail||typeof detail!=='object')return'';
    var rows='';
    Object.keys(detail).forEach(function(k){
        var v=detail[k];
        if(v===null||v===undefined||v==='')return;
        var text;
        if(typeof v==='object')text=JSON.stringify(v);
        else if(typeof v==='boolean')text=v?'是':'否';
        else text=String(v);
        rows+='<div style="display:flex;gap:10px;padding:3px 0;font-size:.78rem;border-top:1px dashed var(--border)">'+
              '<span style="color:var(--muted);min-width:110px">'+esc(k)+'</span>'+
              '<span style="flex:1;word-break:break-all">'+esc(text)+'</span></div>';
    });
    return rows;
}

function renderHealthBody(data){
    var m=healthMeta(data.overall);
    var h='<div class="card" style="margin-bottom:16px;border:2px solid '+m.color+'">'+
        '<div style="display:flex;align-items:center;gap:10px">'+
        '<span style="width:12px;height:12px;border-radius:50%;background:'+m.dot+';box-shadow:0 0 8px '+m.dot+'"></span>'+
        '<strong style="font-size:1.05rem">总体 '+m.text+'</strong>'+
        '<span style="flex:1;font-size:.8rem;color:var(--muted)">检查于 '+formatTime(data.checked_at)+'</span>'+
        '<button class="btn btn-sm" onclick="refreshSystemHealth()">↻ 刷新</button></div></div>';

    h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px">';
    data.checks.forEach(function(c){
        var cm=healthMeta(c.level);
        h+='<div class="card" style="border-left:4px solid '+cm.color+'">'+
           '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">'+
           '<span style="width:8px;height:8px;border-radius:50%;background:'+cm.dot+'"></span>'+
           '<strong style="flex:1">'+esc(c.label)+'</strong>'+
           '<span class="badge" style="background:'+cm.dot+'1f;color:'+cm.color+'">'+cm.text+'</span></div>'+
           '<div style="font-size:.86rem;margin-bottom:8px;word-break:break-all">'+esc(c.summary)+'</div>'+
           healthDetailRows(c.detail)+
           '</div>';
    });
    h+='</div>';
    return h;
}

async function refreshSystemHealth(){
    var body=document.getElementById('health-body');
    if(!body)return;
    var resp=await api(API_BASE+'/system-health');
    if(!resp||!resp.success){body.innerHTML=failBox(resp,'健康检查失败');return}
    body.innerHTML=renderHealthBody(resp.data);
}

async function loadSystemHealth(){
    if(healthTimer){clearInterval(healthTimer);healthTimer=null}
    var main=document.getElementById('main-content');
    main.innerHTML='<div class="page-header"><h2>🩺 系统健康</h2>'+
        '<p>数据库、磁盘、工作流、LLM 配置与后台任务的即时状态</p></div>'+
        '<div id="health-body"><div class="empty-state">检查中...</div></div>';
    await refreshSystemHealth();
    // 停在健康页时每 30 秒自刷一次；切走就自己停，避免后台空转
    healthTimer=setInterval(function(){
        if(currentPage!=='system-health'){clearInterval(healthTimer);healthTimer=null;return}
        if(document.hidden)return;
        refreshSystemHealth();
    },30000);
}

//  文件管理页面
// ═══════════════════════════════════════
async function loadFileManagement(){
    var c=document.getElementById('main-content');
    c.innerHTML='<div class="page-header"><h2>📂 文件管理</h2><p>查看系统文件存储统计与保留策略</p></div><div id="file-stats-content" style="text-align:center;padding:40px;color:var(--muted)">加载中...</div>';
    try{
        var d=await api(API_BASE+'/file-stats','GET');
        if(!d||!d.success||!d.data){c.innerHTML='<div class="page-header"><h2>📂 文件管理</h2></div>'+failBox(d,'文件统计加载失败');return}
        var s=d.data;
        var h='<div class="page-header"><h2>📂 文件管理</h2><p>保留策略：工作流完成后 <strong>90天</strong> 自动清理文件和数据库记录（依据《数据安全法》）</p></div>';
        // 总览卡片
        h+='<div class="dashboard-grid">';
        h+='<div class="stat-card"><div class="value">'+s.total_files+'</div><div class="label">文件总数</div></div>';
        h+='<div class="stat-card"><div class="value">'+s.total_size_mb+'</div><div class="label">总容量 (MB)</div></div>';
        h+='<div class="stat-card"><div class="value">'+s.expiring_in_90_days+'</div><div class="label">90天内到期</div></div>';
        h+='<div class="stat-card"><div class="value">'+s.retention_days+'</div><div class="label">保留天数</div></div>';
        h+='</div>';
        // 按类型统计
        h+='<div class="content-panel"><div class="panel-title">按文件类型统计</div><div class="panel-content"><table class="table-view"><thead><tr><th>类型</th><th>数量</th><th>容量 (MB)</th></tr></thead><tbody>';
        for(var i=0;i<(s.by_type||[]).length;i++){var t=s.by_type[i];h+='<tr><td>'+esc(t.type)+'</td><td>'+t.count+'</td><td>'+t.size_mb+'</td></tr>'}
        h+='</tbody></table></div></div>';
        // 按上传类别统计
        if(s.by_upload_type&&s.by_upload_type.length){
            h+='<div class="content-panel"><div class="panel-title">按上传类别统计</div><div class="panel-content"><table class="table-view"><thead><tr><th>类别</th><th>数量</th></tr></thead><tbody>';
            for(var i=0;i<s.by_upload_type.length;i++){var u=s.by_upload_type[i];h+='<tr><td>'+esc(u.type)+'</td><td>'+u.count+'</td></tr>'}
            h+='</tbody></table></div></div>';
        }
        c.innerHTML=h;
    }catch(e){c.innerHTML='<div class="page-header"><h2>📂 文件管理</h2></div><div class="empty-state">加载失败: '+esc(e.message||'')+'</div>'}
}

// ═══════════════════════════════════════
//  数据清理页面
// ═══════════════════════════════════════
async function loadCleanupPage(){
    var c=document.getElementById('main-content');
    c.innerHTML='<div class="page-header"><h2>🗑 数据清理</h2><p>管理过期文件和卡住工作流的清理</p></div><div style="text-align:center;padding:40px;color:var(--muted)">加载中...</div>';
    try{
        var preview=await api(API_BASE+'/cleanup/preview','GET');
        if(!preview||!preview.success){c.innerHTML='<div class="page-header"><h2>🗑 数据清理</h2></div><div class="empty-state">加载预览失败</div>';return}
        var p=preview.data;
        var h='<div class="page-header"><h2>🗑 数据清理</h2><p>截止日期: '+esc(p.cutoff_date||'')+' | 保留天数: '+p.retention_days+'天</p></div>';
        // 预览卡片
        h+='<div class="dashboard-grid">';
        h+='<div class="stat-card"><div class="value">'+p.expired_sessions+'</div><div class="label">过期工作流</div></div>';
        h+='<div class="stat-card"><div class="value">'+p.expired_files+'</div><div class="label">过期文件</div></div>';
        h+='<div class="stat-card"><div class="value">'+p.expired_reports+'</div><div class="label">过期报告</div></div>';
        h+='<div class="stat-card"><div class="value">'+p.estimated_disk_mb+' MB</div><div class="label">预计释放磁盘</div></div>';
        h+='</div>';
        // 操作按钮
        h+='<div class="content-panel"><div class="panel-title">清理操作</div><div class="panel-content" style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">';
        h+='<button class="btn btn-primary btn-lg" onclick="executeCleanup()" id="cleanup-exec-btn">🔧 执行清理（90天）</button>';
        h+='<button class="btn btn-lg" onclick="executeCleanup(30)" style="background:var(--warning-bg);border-color:#f59e0b;color:var(--warning-fg)">⚡ 执行清理（30天）</button>';
        h+='<button class="btn btn-lg" onclick="cleanupStuckWorkflows()" style="background:var(--danger-bg);border-color:#ef4444;color:var(--danger-fg)">⚠ 清理卡住工作流</button>';
        h+='</div><div id="cleanup-result" style="margin-top:12px;font-size:0.85rem"></div></div>';
        // 说明
        h+='<div class="content-panel" style="background:var(--warning-bg);border:1px solid var(--warning)"><div class="panel-title">清理说明</div><div class="panel-content" style="font-size:0.85rem;color:var(--warning-fg)">';
        h+='<ul style="padding-left:20px;line-height:1.8"><li>系统<strong>每天凌晨3点</strong>自动执行清理任务</li><li>清理对象：已完成/失败的过期工作流、关联文件、Token记录、操作日志等</li><li>卡住工作流：超过24小时仍未完成的运行中工作流</li><li>依据《数据安全法》要求，数据保留90天后自动清除</li><li><strong>重要数据请及时导出！</strong></li></ul>';
        h+='</div></div>';
        c.innerHTML=h;
    }catch(e){c.innerHTML='<div class="page-header"><h2>🗑 数据清理</h2></div><div class="empty-state">加载失败: '+esc(e.message||'')+'</div>'}
}

async function executeCleanup(days){
    var btn=document.getElementById('cleanup-exec-btn');if(btn){btn.disabled=true;btn.textContent='执行中...'}
    try{
        var body=days?{retention_days:days}:{};
        var r=await api(API_BASE+'/cleanup/execute','POST',body);
        var el=document.getElementById('cleanup-result');
        if(r&&r.success&&r.data){
            var d=r.data;
            el.innerHTML='<div style="padding:12px;background:var(--success-bg);border-radius:8px;color:var(--success-fg)">✅ 清理完成: 删除文件 '+d.deleted_files+' 个, 释放磁盘 '+(d.freed_disk_bytes/(1024*1024)).toFixed(2)+' MB'+(d.errors&&d.errors.length>0?'<br>⚠ 警告: '+d.errors.join(', '):'')+'</div>';
            showToast('清理完成');
            // 重新加载预览
            setTimeout(function(){loadCleanupPage()},800);
        }else{el.innerHTML='<div style="padding:12px;background:var(--danger-bg);border-radius:8px;color:var(--danger-fg)">❌ 清理失败</div>'}
    }catch(e){
        var el=document.getElementById('cleanup-result');if(el)el.innerHTML='<div style="padding:12px;background:var(--danger-bg);border-radius:8px;color:var(--danger-fg)">❌ '+esc(e.message||'清理失败')+'</div>';
    }
    var btn=document.getElementById('cleanup-exec-btn');if(btn){btn.disabled=false;btn.textContent='🔧 执行清理（90天）'}
}

async function cleanupStuckWorkflows(){
    if(!confirm('确定要标记所有超过24小时的运行中工作流为失败状态吗？'))return;
    try{
        var r=await api(API_BASE+'/cleanup/fix-failed?max_age_hours=24','POST');
        if(r&&r.success){showToast('已清理 '+r.data.cleaned_count+' 个卡住工作流','success');loadCleanupPage()}else{showToast('清理失败','error')}
    }catch(e){showToast('清理失败: '+e.message,'error')}
}
