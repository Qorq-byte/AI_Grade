/* 云海睿评 · 登录页脚本
 * 抽离后页面可 no-store，脚本走 /static 协商缓存。
 * 注意：Tailwind 的配置块仍留在页面里（必须在 CDN 之后、且在样式生成之前）。
 */
        // ===== REDIRECT LOGIC =====
        var REDIRECT_URL = getQueryParam('redirect') || getQueryParam('next') || '/client';

        function getQueryParam(name) {
            const params = new URLSearchParams(window.location.search);
            return params.get(name);
        }

        // ===== GLOBAL STATE =====
        let mouseX = 0, mouseY = 0;
        let isTyping = false;
        let isLookingAtEachOther = false;
        let isPurplePeeking = false;
        let showPassword = false;
        let passwordLength = 0;
        const purpleChar = document.getElementById('purpleChar');
        const purpleEyes = document.getElementById('purpleEyes');
        const blackEyes = document.getElementById('blackEyes');
        const orangeEyes = document.getElementById('orangeEyes');
        const yellowEyes = document.getElementById('yellowEyes');

        // ===== MOUSE TRACKING =====
        document.addEventListener('mousemove', (e) => {
            mouseX = e.clientX;
            mouseY = e.clientY;
            updateAllEyes();
        });

        function getEyeCenter(el) {
            const r = el.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }

        function calcPupil(el, maxDist, fx, fy) {
            if (fx !== undefined && fy !== undefined) return { x: fx, y: fy };
            const c = getEyeCenter(el);
            const dx = mouseX - c.x, dy = mouseY - c.y;
            const dist = Math.min(Math.sqrt(dx * dx + dy * dy), maxDist);
            const angle = Math.atan2(dy, dx);
            return { x: Math.cos(angle) * dist, y: Math.sin(angle) * dist };
        }

        function updateAllEyes() {
            purpleEyes.querySelectorAll('.eye-white .pupil').forEach(p => {
                const pos = calcPupil(p.parentElement, 5,
                    (showPassword && passwordLength > 0) ? (isPurplePeeking ? 4 : -4) : (isLookingAtEachOther ? 3 : undefined),
                    (showPassword && passwordLength > 0) ? (isPurplePeeking ? 5 : -4) : (isLookingAtEachOther ? 4 : undefined));
                p.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
            });

            blackEyes.querySelectorAll('.eye-white .pupil').forEach(p => {
                const pos = calcPupil(p.parentElement, 4,
                    (showPassword && passwordLength > 0) ? -4 : (isLookingAtEachOther ? 0 : undefined),
                    (showPassword && passwordLength > 0) ? -4 : (isLookingAtEachOther ? -4 : undefined));
                p.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
            });

            const orangeChar = document.getElementById('orangeChar');
            orangeEyes.querySelectorAll('.pupil').forEach(p => {
                const pos = calcPupil(orangeChar, 5,
                    (showPassword && passwordLength > 0) ? -5 : undefined,
                    (showPassword && passwordLength > 0) ? -4 : undefined);
                p.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
            });

            const yellowChar = document.getElementById('yellowChar');
            yellowEyes.querySelectorAll('.pupil').forEach(p => {
                const pos = calcPupil(yellowChar, 5,
                    (showPassword && passwordLength > 0) ? -5 : undefined,
                    (showPassword && passwordLength > 0) ? -4 : undefined);
                p.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
            });
        }

        function calcBodySkew(el) {
            const r = el.getBoundingClientRect();
            const dx = mouseX - (r.left + r.width / 2);
            return Math.max(-6, Math.min(6, -dx / 120));
        }

        function updateBodyLean() {
            const blackChar = document.getElementById('blackChar');
            const orangeChar = document.getElementById('orangeChar');
            const yellowChar = document.getElementById('yellowChar');

            if (showPassword && passwordLength > 0) {
                purpleChar.style.transform = 'skewX(0deg)';
                blackChar.style.transform = 'skewX(0deg)';
                orangeChar.style.transform = 'skewX(0deg)';
                yellowChar.style.transform = 'skewX(0deg)';
            } else if (isLookingAtEachOther) {
                purpleChar.style.transform = `skewX(${calcBodySkew(purpleChar)-12}deg) translateX(40px)`;
                blackChar.style.transform = `skewX(${calcBodySkew(blackChar)*1.5+10}deg) translateX(20px)`;
                orangeChar.style.transform = `skewX(${calcBodySkew(orangeChar)}deg)`;
                yellowChar.style.transform = `skewX(${calcBodySkew(yellowChar)}deg)`;
            } else if (isTyping) {
                purpleChar.style.transform = `skewX(${calcBodySkew(purpleChar)-12}deg) translateX(40px)`;
                blackChar.style.transform = `skewX(${calcBodySkew(blackChar)*1.5}deg)`;
                orangeChar.style.transform = `skewX(${calcBodySkew(orangeChar)}deg)`;
                yellowChar.style.transform = `skewX(${calcBodySkew(yellowChar)}deg)`;
            } else {
                purpleChar.style.transform = `skewX(${calcBodySkew(purpleChar)}deg)`;
                blackChar.style.transform = `skewX(${calcBodySkew(blackChar)}deg)`;
                orangeChar.style.transform = `skewX(${calcBodySkew(orangeChar)}deg)`;
                yellowChar.style.transform = `skewX(${calcBodySkew(yellowChar)}deg)`;
            }
            updateAllEyes();
        }

        setInterval(updateBodyLean, 100);

        function onFieldFocus() {
            isTyping = true;
            isLookingAtEachOther = true;
            purpleChar.style.height = '440px';
            purpleEyes.style.left = '55px'; purpleEyes.style.top = '65px';
            blackEyes.style.left = '32px'; blackEyes.style.top = '12px';
            updateAllEyes();
        }

        function onFieldBlur() {
            isTyping = false;
            setTimeout(() => {
                isLookingAtEachOther = false;
                purpleChar.style.height = '400px';
                purpleEyes.style.left = '45px'; purpleEyes.style.top = '40px';
                blackEyes.style.left = '26px'; blackEyes.style.top = '32px';
                updateAllEyes();
            }, 200);
        }

        function onFieldInput() {
            isTyping = isLookingAtEachOther = true;
            purpleChar.style.height = '440px';
            purpleEyes.style.left = '55px'; purpleEyes.style.top = '65px';
            blackEyes.style.left = '32px'; blackEyes.style.top = '12px';
            updateAllEyes();
            clearTimeout(window._typingTimer);
            window._typingTimer = setTimeout(() => {
                isLookingAtEachOther = false;
                if (isTyping) { purpleChar.style.height = '440px'; }
                updateAllEyes();
            }, 1000);
        }

        function onPasswordInput() {
            passwordLength = (document.getElementById('loginPassword').value || '').length;
            if (!showPassword && passwordLength > 0) {
                isTyping = isLookingAtEachOther = true;
                purpleChar.style.height = '440px';
                purpleEyes.style.left = '55px'; purpleEyes.style.top = '65px';
                blackEyes.style.left = '32px'; blackEyes.style.top = '12px';
            }
            if (showPassword && passwordLength > 0) {
                purpleChar.style.height = '400px';
                purpleEyes.style.left = '20px'; purpleEyes.style.top = '35px';
                blackEyes.style.left = '10px'; blackEyes.style.top = '28px';
                orangeEyes.style.left = '50px'; orangeEyes.style.top = '85px';
                yellowEyes.style.left = '20px'; yellowEyes.style.top = '35px';
            }
            updateAllEyes();
        }

        function togglePassword(fieldId, btn) {
            const field = document.getElementById(fieldId);
            const vis = field.type === 'text';
            field.type = vis ? 'password' : 'text';
            showPassword = !vis;
            passwordLength = field.value.length;
            const icon = btn.querySelector('i');
            icon.setAttribute('data-lucide', showPassword ? 'eye-off' : 'eye');
            lucide.createIcons();
            if (showPassword && passwordLength > 0) {
                purpleChar.style.height = '400px';
                purpleEyes.style.left = '20px'; purpleEyes.style.top = '35px';
                blackEyes.style.left = '10px'; blackEyes.style.top = '28px';
                orangeEyes.style.left = '50px'; orangeEyes.style.top = '85px';
                yellowEyes.style.left = '20px'; yellowEyes.style.top = '35px';
            }
            updateAllEyes();
        }

        // Blinking
        (function blink(id) {
            const fn = () => {
                const eyes = document.querySelectorAll('#' + id + ' .eye-white');
                eyes.forEach(e => e.style.height = '2px');
                setTimeout(() => {
                    eyes.forEach(e => e.style.height = '');
                    setTimeout(fn, Math.random() * 4000 + 3000);
                }, 150);
            };
            setTimeout(fn, Math.random() * 4000 + 3000);
        })('purpleEyes');

        (function blink(id) {
            const fn = () => {
                const eyes = document.querySelectorAll('#' + id + ' .eye-white');
                eyes.forEach(e => e.style.height = '2px');
                setTimeout(() => {
                    eyes.forEach(e => e.style.height = '');
                    setTimeout(fn, Math.random() * 4000 + 3000);
                }, 150);
            };
            setTimeout(fn, Math.random() * 4000 + 3000);
        })('blackEyes');

        // Peeking
        setInterval(() => {
            if (showPassword && passwordLength > 0 && !isPurplePeeking) {
                setTimeout(() => {
                    isPurplePeeking = true; updateAllEyes();
                    setTimeout(() => { isPurplePeeking = false; updateAllEyes(); }, 800);
                }, Math.random() * 3000 + 2000);
            }
        }, 5000);

        // ===== PASSWORD STRENGTH =====
        function validatePasswordStrength(password) {
            const errors = [];
            if (password.length < 8) errors.push('密码长度至少8位');
            let types = 0;
            if (/[a-z]/.test(password)) types++;
            if (/[A-Z]/.test(password)) types++;
            if (/\d/.test(password)) types++;
            if (/[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?`~]/.test(password)) types++;
            if (types < 3) errors.push('密码需包含大写字母、小写字母、数字、特殊字符中至少3种');
            if (/\s/.test(password)) errors.push('密码不能包含空格');
            return errors;
        }

        function onRegPasswordInput() {
            passwordLength = document.getElementById('regPassword').value.length;
            const pw = document.getElementById('regPassword').value;
            const bar = document.getElementById('pwStrengthBar');
            const text = document.getElementById('pwStrengthText');
            const reqs = document.getElementById('pwRequirements');
            const reqLen = document.getElementById('reqLen');
            const reqTypes = document.getElementById('reqTypes');
            const reqSpace = document.getElementById('reqSpace');

            if (!pw) { bar.style.width = '0%'; text.textContent = ''; reqs.style.display = 'none'; return; }
            reqs.style.display = 'block';

            const lenOk = pw.length >= 8;
            let typesOk = 0;
            if (/[a-z]/.test(pw)) typesOk++;
            if (/[A-Z]/.test(pw)) typesOk++;
            if (/\d/.test(pw)) typesOk++;
            if (/[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?`~]/.test(pw)) typesOk++;
            const spaceOk = !/\s/.test(pw);

            reqLen.style.color = lenOk ? '#16a34a' : '#ef4444';
            reqTypes.style.color = typesOk >= 3 ? '#16a34a' : '#ef4444';
            reqSpace.style.color = spaceOk ? '#16a34a' : '#ef4444';

            const errors = validatePasswordStrength(pw);
            if (errors.length === 0) {
                bar.style.width = '100%'; bar.style.background = '#16a34a';
                text.textContent = '密码强度：强'; text.style.color = '#16a34a';
            } else if (errors.length <= 1) {
                bar.style.width = '66%'; bar.style.background = '#f59e0b';
                text.textContent = '密码强度：中'; text.style.color = '#f59e0b';
            } else {
                bar.style.width = '33%'; bar.style.background = '#ef4444';
                text.textContent = '密码强度：弱'; text.style.color = '#ef4444';
            }

            if (!showPassword && passwordLength > 0) {
                isTyping = isLookingAtEachOther = true;
                purpleChar.style.height = '440px';
                purpleEyes.style.left = '55px'; purpleEyes.style.top = '65px';
                blackEyes.style.left = '32px'; blackEyes.style.top = '12px';
            }
            updateAllEyes();
        }

        function switchTab(tab) {
            document.getElementById('tabLogin').classList.toggle('active', tab === 'login');
            document.getElementById('tabRegister').classList.toggle('active', tab === 'register');
            document.getElementById('loginForm').classList.toggle('active', tab === 'login');
            document.getElementById('registerForm').classList.toggle('active', tab === 'register');
            hideAllMessages();
            isTyping = isLookingAtEachOther = false;
            showPassword = false; passwordLength = 0;
            purpleChar.style.height = '400px';
            purpleEyes.style.left = '45px'; purpleEyes.style.top = '40px';
            blackEyes.style.left = '26px'; blackEyes.style.top = '32px';
            orangeEyes.style.left = '82px'; orangeEyes.style.top = '90px';
            yellowEyes.style.left = '52px'; yellowEyes.style.top = '40px';
            updateAllEyes();
        }

        function hideAllMessages() {
            ['loginError','loginSuccess','registerError','registerSuccess'].forEach(id => {
                document.getElementById(id).classList.remove('show');
            });
        }

        function showMessage(id, msg, type) {
            const el = document.getElementById(id);
            el.textContent = msg;
            el.classList.add('show');
            if (type === 'success') { el.classList.add('success-msg'); el.classList.remove('error-msg'); }
            else { el.classList.add('error-msg'); el.classList.remove('success-msg'); }
            if (type !== 'success') setTimeout(() => el.classList.remove('show'), 5000);
        }

        // ===== LOGIN =====
        async function handleLogin(e) {
            e.preventDefault();
            hideAllMessages();
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            const remember = document.getElementById('rememberMe').checked;
            const btn = document.getElementById('loginBtn');

            if (!username || !password) {
                showMessage('loginError', '请输入用户名和密码', 'error');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<i data-lucide="loader" style="width:20px;height:20px;animation:spin 1s linear infinite;"></i> 登录中...';
            lucide.createIcons();

            try {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password }),
                });

                const data = await res.json();

                if (res.ok && data.success) {
                    const u = data.data;
                    const userInfo = JSON.stringify({ id: u.user_id, username: u.username, role: u.role });
                    sessionStorage.setItem('token', u.access_token);
                    sessionStorage.setItem('refresh_token', u.refresh_token);
                    sessionStorage.setItem('user', userInfo);

                    if (remember) {
                        localStorage.setItem('token', u.access_token);
                        localStorage.setItem('refresh_token', u.refresh_token);
                        localStorage.setItem('user', userInfo);
                    }

                    showMessage('loginSuccess',
                        `登录成功！欢迎，${u.username}（${u.role === 'admin' ? '管理员' : '普通用户'}），正在跳转...`,
                        'success');

                    // 根据角色决定跳转页面
                    let redirectUrl = REDIRECT_URL;
                    if (u.role === 'admin') {
                        redirectUrl = '/admin';
                    } else if (redirectUrl === '/admin') {
                        redirectUrl = '/client';
                    }

                    // 初始口令未改：除改密接口外后端一律 403，直接送到设置页的改密面板。
                    // 管理员也一样 —— 默认管理员口令写在源码里，必须先改掉。
                    if (u.must_change_password) {
                        sessionStorage.setItem('must_change_password', '1');
                        redirectUrl = '/client/#settings';
                        showMessage('loginSuccess',
                            '检测到您正在使用初始密码，请先修改密码后继续使用',
                            'success');
                    } else {
                        sessionStorage.removeItem('must_change_password');
                    }

                    setTimeout(() => {
                        window.location.href = redirectUrl;
                    }, 1200);
                } else {
                    showMessage('loginError', data.detail || '用户名或密码错误，请重试', 'error');
                }
            } catch (err) {
                showMessage('loginError', '无法连接到服务器，请确认后端已启动', 'error');
            }

            btn.disabled = false;
            btn.innerHTML = '登 录';
        }

        // ===== REGISTER =====
        async function handleRegister(e) {
            e.preventDefault();
            hideAllMessages();
            const username = document.getElementById('regUsername').value.trim();
            const phone = document.getElementById('regPhone').value.trim();
            const password = document.getElementById('regPassword').value;
            const confirmPassword = document.getElementById('regConfirmPassword').value;
            const btn = document.getElementById('registerBtn');

            if (!username || !password || !confirmPassword) {
                showMessage('registerError', '请填写必填字段', 'error');
                return;
            }

            const pwErrors = validatePasswordStrength(password);
            if (pwErrors.length > 0) {
                showMessage('registerError', pwErrors.join('；'), 'error');
                return;
            }

            if (password !== confirmPassword) {
                showMessage('registerError', '两次输入的密码不一致', 'error');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<i data-lucide="loader" style="width:20px;height:20px;animation:spin 1s linear infinite;"></i> 注册中...';
            lucide.createIcons();

            try {
                const body = { username, password };
                if (phone) body.phone = phone;

                const res = await fetch('/api/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });

                const data = await res.json();

                if (res.ok && data.success) {
                    showMessage('registerSuccess',
                        `注册成功！账号：${data.data.username}，即将切换...`,
                        'success');

                    document.getElementById('regUsername').value = '';
                    document.getElementById('regPhone').value = '';
                    document.getElementById('regPassword').value = '';
                    document.getElementById('regConfirmPassword').value = '';
                    document.getElementById('pwStrengthBar').style.width = '0%';
                    document.getElementById('pwStrengthText').textContent = '';
                    document.getElementById('pwRequirements').style.display = 'none';

                    setTimeout(() => {
                        switchTab('login');
                        document.getElementById('loginUsername').value = data.data.username;
                    }, 1500);
                } else {
                    showMessage('registerError', data.detail || '注册失败，请重试', 'error');
                }
            } catch (err) {
                showMessage('registerError', '无法连接到服务器，请确认后端已启动', 'error');
            }

            btn.disabled = false;
            btn.innerHTML = '注 册';
        }

        lucide.createIcons();
        updateAllEyes();
        setInterval(updateAllEyes, 50);
    