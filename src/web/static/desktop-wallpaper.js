(function () {
    'use strict';

    // Вода считается в уменьшенной текстуре, мелкая взвесь - отдельными точками.
    // Один requestAnimationFrame, без сторожей и смены качества во время работы.
    var MAX_PIXELS = 1920 * 1080;
    var WATER_PIXELS = 960 * 540;
    var PARTICLE_DENSITY = 0.7; // на 30% реже исходной плотности
    var MAX_PARTICLES = 2200;

    function main() {
        var canvas = /** @type {HTMLCanvasElement | null} */ (document.getElementById('desktop-sea'));
        if (!canvas) return;
        var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
        var preference = null;
        try { preference = localStorage.getItem('dno-wallpaper-motion'); } catch {}
        var animated = preference === null ? !motion.matches : preference === 'on';
        var available = false;
        function publish() { window.dispatchEvent(new CustomEvent('dno:wallpaper')); }
        window.DNOWallpaper = {
            get animated() { return animated && available; },
            get available() { return available; },
            setAnimated: function (value) {
                animated = !!value;
                preference = animated ? 'on' : 'off';
                try { localStorage.setItem('dno-wallpaper-motion', preference); } catch {}
                publish();
                sync();
            }
        };
        var gl = null;
        try {
            gl = canvas.getContext('webgl', {
                alpha: false, antialias: false, depth: false, stencil: false,
                preserveDrawingBuffer: false
            });
        } catch {}
        if (!gl) { canvas.style.display = 'none'; publish(); return; }

        var format = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
        var precision = 'precision ' + (format && format.precision ? 'highp' : 'mediump') + ' float;\n';
        var quadVS = [
            'attribute vec2 p;',
            'varying mediump vec2 v_uv;',
            'void main(){v_uv=p*.5+.5;gl_Position=vec4(p,0.,1.);}'
        ].join('\n');
        var waterFS = precision + [
            'varying mediump vec2 v_uv;',
            'uniform vec2 u_size;uniform float u_time;uniform vec2 u_mouse;',
            'uniform vec4 u_rays[7];uniform vec4 u_waves[3];',
            // Hash без sin: существенно дешевле в шуме на каждом пикселе.
            'float hash(vec2 p){',
            ' vec3 q=fract(vec3(p.xyx)*.1031);q+=dot(q,q.yzx+33.33);',
            ' return fract((q.x+q.y)*q.z);}',
            'float noise(vec2 p){',
            ' vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);',
            ' return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),',
            ' mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),f.x),f.y);}',
            'float fog(vec2 p){',
            ' mat2 r=mat2(.8525,-.5227,.5227,.8525);',
            ' float a=.5,v=0.;',
            ' for(int i=0;i<3;i++){v+=a*noise(p);p=r*p*2.03+11.7;a*=.5;}',
            ' return v;}',
            // Высота и наклон поверхности. Фокусировку лучей считаем на CPU.
            'vec3 surface(vec2 pos,float t){',
            ' vec3 value=vec3(0.);',
            ' for(int i=0;i<3;i++){',
            '  vec4 w=u_waves[i];float phase=dot(w.xy,pos)-w.w*t+float(i)*2.13;',
            '  float s=sin(phase),c=cos(phase);',
            '  value+=vec3(w.z*s,w.z*w.xy*c);',
            ' }',
            ' return value;}',
            'void main(){',
            ' vec2 uv=v_uv,p=vec2(uv.x*u_size.x/u_size.y,uv.y);',
            ' float t=u_time,depth=uv.y;',
            ' float travel=1.-depth,litDepth=smoothstep(.24,.82,depth);',
            ' vec3 wave=vec3(0.);',
            ' if(litDepth>0.) wave=surface(vec2(p.x,travel*2.6)+u_mouse*vec2(.03,.02),t);',
            ' vec2 q=p+wave.yz*.028*litDepth;',
            ' float g1=smoothstep(0.,.55,depth),g2=smoothstep(.45,1.,depth);',
            ' vec3 col=mix(vec3(.006,.052,.088),vec3(.018,.105,.165),g1);',
            ' col=mix(col,vec3(.055,.185,.255),g2*g2);',
            ' float farFog=fog((q+vec2(t*.012,-t*.036)+u_mouse*.02)*vec2(1.4,2.));',
            ' float nearFog=fog((q+vec2(t*.03,-t*.035)+u_mouse*.06)*vec2(2.6,3.8)+4.7);',
            ' col+=vec3(.05,.13,.17)*farFog*(.25+.55*g1);',
            ' col+=vec3(.04,.11,.15)*nearFog*smoothstep(.15,.85,depth)*.8;',
            // У темного дна не считаем отражения и семь световых пучков вообще.
            ' if(litDepth>0.){',
            ' vec3 normal=normalize(vec3(-wave.y,1.,-wave.z));',
            // Ровное верхнее свечение вместо залитых светом гребней-овалов.
            ' float surf=exp(-travel*4.2);',
            ' col+=vec3(.30,.55,.65)*surf*.52*litDepth;',
            // Френель: мягкие скользящие отражения, а не белая полоса сверху.
            ' float grazing=1.-clamp(dot(normal,normalize(vec3(.1,.28,.95))),0.,1.);',
            ' float fresnel=.02+.98*grazing*grazing*grazing*grazing*grazing;',
            ' col+=vec3(.10,.18,.20)*surf*fresnel*.08*litDepth;',
            ' float rays=0.,vertical=litDepth*exp(-travel*2.9);',
            ' float bend=(wave.y*.01+(farFog-.45)*.012)*travel*litDepth;',
            ' float spread=1.+travel*.4,inverseSpread2=1./(spread*spread);',
            ' for(int i=0;i<7;i++){',
            '  vec4 ray=u_rays[i];float dx=uv.x-ray.x-travel*ray.w+bend;',
            // Свет рассеивается шире по мере удаления от поверхности.
            '  float distance2=dx*dx*ray.y*inverseSpread2;',
            // Мягкий поперечный профиль, без отдельного круглого ореола.
            '  rays+=exp(-distance2)*ray.z;',
            ' }',
            ' float transmission=mix(.6,1.,smoothstep(.2,.75,farFog*.75+nearFog*.4));',
            ' col+=vec3(.40,.66,.76)*rays*vertical*transmission*(.6+.2*surf)*.68;',
            ' }',
            ' col*=mix(.22,1.,smoothstep(0.,.5,depth));',
            ' col+=vec3(.004,.009,.014);',
            ' col=col/(1.+col*.32)*1.05;',
            ' col+=(hash(gl_FragCoord.xy)-.5)/255.;',
            ' gl_FragColor=vec4(col,1.);',
            '}'
        ].join('\n');
        var compositeFS = precision + [
            'varying mediump vec2 v_uv;uniform sampler2D u_water;',
            'float grain(vec2 p){',
            ' vec3 q=fract(vec3(p.xyx)*.1031);q+=dot(q,q.yzx+33.33);',
            ' return fract((q.x+q.y)*q.z);}',
            'void main(){',
            ' vec3 col=texture2D(u_water,v_uv).rgb;',
            ' float vignette=1.-smoothstep(.25,.72,length(v_uv-.5));',
            ' col*=mix(.64,1.,vignette);',
            // Статичный дизеринг без скачка зерна каждую секунду.
            ' col+=(grain(gl_FragCoord.xy)-.5)/255.;',
            ' gl_FragColor=vec4(col,1.);',
            '}'
        ].join('\n');
        var dustVS = [
            'attribute vec2 a_position;attribute vec4 a_data;attribute vec2 a_motion;',
            'uniform vec2 u_size;uniform float u_time,u_ratio;uniform vec2 u_mouse;',
            'varying mediump float v_alpha;',
            'void main(){',
            ' float y=mod(a_position.y-u_time*a_data.y/u_size.y+.01,1.02)-.01;',
            ' y+=sin(u_time*.22+a_data.w)*a_motion.x*.22/u_size.y;',
            ' float drift=sin(u_time*.3+a_data.w)*a_motion.x;',
            ' float x=a_position.x+(drift+u_mouse.x*a_motion.x)/u_size.x;',
            ' gl_Position=vec4(x*2.-1.,y*2.-1.,0.,1.);',
            ' gl_PointSize=max(1.,a_data.x*u_ratio);',
            ' v_alpha=a_data.z*(.78+.22*sin(u_time*a_motion.y+a_data.w));',
            ' v_alpha*=.55+.45*smoothstep(0.,.85,y);',
            '}'
        ].join('\n');
        var dustFS = precision + [
            'varying mediump float v_alpha;',
            'void main(){',
            ' float edge=1.-smoothstep(.1,.5,length(gl_PointCoord-.5));',
            ' gl_FragColor=vec4(.68,.86,.94,edge*v_alpha);',
            '}'
        ].join('\n');

        var resources = null;
        var frameId = null;
        var lastTimestamp = null;
        var time = 0;
        var pageActive = true;
        var contextLost = false;
        var sizeDirty = true;
        var particlesDirty = true;
        var width = 0, height = 0, ratio = 1;
        var waterWidth = 0, waterHeight = 0;
        var particleCount = 0;
        var targetX = 0, targetY = 0, mouseX = 0, mouseY = 0;
        var rays = new Float32Array(28);
        // kx, kz, амплитуда, угловая скорость: общие параметры CPU и GPU.
        var waves = new Float32Array([
            3.8, 1.3, .035, .55,
            7.2, -3.9, .019, -.72,
            15.1, 6.2, .008, 1.05
        ]);
        var rayOrigins = [.025, .145, .30, .465, .595, .78, .955];
        // Четыре широких мягких пучка и три тонкие световые прожилки.
        var rayWidths = [.016, .009, .024, .018, .008, .021, .010];
        var surfaceSample = new Float32Array(4);
        var lightLength = Math.sqrt(.24 * .24 + 1 + .12 * .12);
        var lightX = .24 / lightLength, lightY = -1 / lightLength, lightZ = .12 / lightLength;
        var maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);

        function compile(type, source) {
            var shader = gl.createShader(type);
            gl.shaderSource(shader, source);
            gl.compileShader(shader);
            if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
                var message = gl.getShaderInfoLog(shader);
                gl.deleteShader(shader);
                throw new Error(message);
            }
            return shader;
        }

        function program(vertex, fragment, attributes, uniforms) {
            var vs = compile(gl.VERTEX_SHADER, vertex);
            var fs;
            var handle;
            try {
                fs = compile(gl.FRAGMENT_SHADER, fragment);
                handle = gl.createProgram();
                gl.attachShader(handle, vs);
                gl.attachShader(handle, fs);
                attributes.forEach(function (name, index) { gl.bindAttribLocation(handle, index, name); });
                gl.linkProgram(handle);
                if (!gl.getProgramParameter(handle, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(handle));
            } catch (error) {
                if (handle) gl.deleteProgram(handle);
                throw error;
            } finally {
                gl.deleteShader(vs);
                if (fs) gl.deleteShader(fs);
            }
            var locations = {};
            uniforms.forEach(function (name) { locations[name] = gl.getUniformLocation(handle, name); });
            return { handle: handle, uniforms: locations };
        }

        function release() {
            if (!resources || contextLost) return;
            ['water', 'composite', 'dust'].forEach(function (name) {
                if (resources[name]) gl.deleteProgram(resources[name].handle);
            });
            gl.deleteBuffer(resources.quad);
            gl.deleteBuffer(resources.particles);
            gl.deleteTexture(resources.texture);
            gl.deleteFramebuffer(resources.framebuffer);
            resources = null;
        }

        function initialize() {
            resources = {};
            resources.water = program(quadVS, waterFS, ['p'], ['u_size', 'u_time', 'u_mouse', 'u_rays[0]', 'u_waves[0]']);
            gl.useProgram(resources.water.handle);
            gl.uniform4fv(resources.water.uniforms['u_waves[0]'], waves);
            resources.composite = program(quadVS, compositeFS, ['p'], ['u_water']);
            resources.dust = program(dustVS, dustFS, ['a_position', 'a_data', 'a_motion'], ['u_size', 'u_time', 'u_ratio', 'u_mouse']);
            resources.quad = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, resources.quad);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
            resources.particles = gl.createBuffer();
            resources.texture = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_2D, resources.texture);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            resources.framebuffer = gl.createFramebuffer();
            gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
            gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, resources.texture, 0);
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            gl.disable(gl.DEPTH_TEST);
            gl.disable(gl.CULL_FACE);
            waterWidth = waterHeight = 0;
            sizeDirty = true;
            particlesDirty = true;
            canvas.style.display = '';
        }

        function particles() {
            particleCount = Math.min(MAX_PARTICLES, Math.round(width * height / 900 * PARTICLE_DENSITY));
            var data = new Float32Array(particleCount * 8);
            var seed = 341;
            function random() {
                seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
                return seed / 4294967296;
            }
            for (var i = 0; i < particleCount; i++) {
                var offset = i * 8;
                var depth = random();
                data[offset] = random();
                data[offset + 1] = random();
                data[offset + 2] = 2 + (1 - depth) * 2.5 + (random() - .5) * .8; // диаметр в CSS px
                data[offset + 3] = 8 + depth * 12; // медленное оседание, px/s
                data[offset + 4] = .12 + depth * .24;
                data[offset + 5] = random() * Math.PI * 2;
                data[offset + 6] = 2 + depth * 6;
                data[offset + 7] = .4 + random() * .6;
            }
            gl.bindBuffer(gl.ARRAY_BUFFER, resources.particles);
            gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
        }

        function resize() {
            if (!sizeDirty) return true;
            var bounds = canvas.getBoundingClientRect();
            if (!bounds.width || !bounds.height) return false;
            if (width !== bounds.width || height !== bounds.height) particlesDirty = true;
            width = bounds.width;
            height = bounds.height;
            ratio = Math.min(window.devicePixelRatio || 1, 1.5,
                Math.sqrt(MAX_PIXELS / (width * height)), maxTextureSize / Math.max(width, height));
            var bw = Math.max(1, Math.floor(width * ratio));
            var bh = Math.max(1, Math.floor(height * ratio));
            if (canvas.width !== bw) canvas.width = bw;
            if (canvas.height !== bh) canvas.height = bh;
            var scale = Math.min(.5, Math.sqrt(WATER_PIXELS / (bw * bh)));
            var ww = Math.max(1, Math.floor(bw * scale));
            var wh = Math.max(1, Math.floor(bh * scale));
            if (waterWidth !== ww || waterHeight !== wh) {
                waterWidth = ww;
                waterHeight = wh;
                gl.bindTexture(gl.TEXTURE_2D, resources.texture);
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, ww, wh, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
                gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
                if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error('Incomplete water framebuffer');
                gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            }
            if (particlesDirty) {
                particles();
                particlesDirty = false;
            }
            sizeDirty = false;
            return true;
        }

        function quad(shader) {
            gl.useProgram(shader.handle);
            gl.bindBuffer(gl.ARRAY_BUFFER, resources.quad);
            gl.enableVertexAttribArray(0);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
            gl.disableVertexAttribArray(1);
            gl.disableVertexAttribArray(2);
        }

        function sampleSurface(x, z) {
            surfaceSample.fill(0);
            for (var i = 0; i < 3; i++) {
                var offset = i * 4;
                var kx = waves[offset], kz = waves[offset + 1], amplitude = waves[offset + 2];
                var phase = kx * x + kz * z - waves[offset + 3] * time + i * 2.13;
                var s = Math.sin(phase), c = Math.cos(phase);
                surfaceSample[0] += amplitude * s;
                surfaceSample[1] += amplitude * kx * c;
                surfaceSample[2] += amplitude * kz * c;
                surfaceSample[3] -= amplitude * (kx * kx + kz * kz) * s;
            }
        }

        function updateRays() {
            var aspect = width / height;
            var eta = 1 / 1.333; // воздух -> вода, закон Снеллиуса
            for (var i = 0; i < 7; i++) {
                sampleSurface(rayOrigins[i] * aspect, (i % 3) * .16);
                var sx = surfaceSample[1], sz = surfaceSample[2];
                var length = Math.sqrt(sx * sx + 1 + sz * sz);
                var nx = -sx / length, ny = 1 / length, nz = -sz / length;
                var dot = nx * lightX + ny * lightY + nz * lightZ;
                var k = 1 - eta * eta * (1 - dot * dot);
                var factor = eta * dot + Math.sqrt(Math.max(0, k));
                var directionX = eta * lightX - factor * nx;
                var directionY = eta * lightY - factor * ny;
                var focus = Math.max(.7, Math.min(1.4, 1 / Math.sqrt(Math.max(.4, 1 + .22 * surfaceSample[3]))));
                var w = rayWidths[i] / focus;
                rays[i * 4] = rayOrigins[i] + surfaceSample[0] * .015 / aspect + mouseX * .015;
                rays[i * 4 + 1] = 1 / (w * w);
                rays[i * 4 + 2] = (.65 + .06 * (i % 3)) * focus * -dot;
                rays[i * 4 + 3] = directionX / Math.max(.2, -directionY) / aspect * .7;
            }
        }

        function render() {
            if (!resize()) return;
            gl.disable(gl.BLEND);
            // Отсоединить текстуру от sampler перед записью в нее.
            gl.bindTexture(gl.TEXTURE_2D, null);
            gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
            gl.viewport(0, 0, waterWidth, waterHeight);
            quad(resources.water);
            var u = resources.water.uniforms;
            updateRays();
            gl.uniform2f(u.u_size, width, height);
            gl.uniform1f(u.u_time, time);
            gl.uniform2f(u.u_mouse, mouseX, mouseY);
            gl.uniform4fv(u['u_rays[0]'], rays);
            gl.drawArrays(gl.TRIANGLES, 0, 3);

            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            gl.viewport(0, 0, canvas.width, canvas.height);
            quad(resources.composite);
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, resources.texture);
            gl.uniform1i(resources.composite.uniforms.u_water, 0);
            gl.drawArrays(gl.TRIANGLES, 0, 3);

            gl.useProgram(resources.dust.handle);
            gl.bindBuffer(gl.ARRAY_BUFFER, resources.particles);
            gl.enableVertexAttribArray(0);
            gl.enableVertexAttribArray(1);
            gl.enableVertexAttribArray(2);
            gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 32, 0);
            gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 32, 8);
            gl.vertexAttribPointer(2, 2, gl.FLOAT, false, 32, 24);
            u = resources.dust.uniforms;
            gl.uniform2f(u.u_size, width, height);
            gl.uniform1f(u.u_time, time);
            gl.uniform1f(u.u_ratio, ratio);
            gl.uniform2f(u.u_mouse, mouseX, mouseY);
            gl.enable(gl.BLEND);
            gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
            gl.drawArrays(gl.POINTS, 0, particleCount);
            gl.disable(gl.BLEND);
        }

        function stop() {
            if (frameId !== null) cancelAnimationFrame(frameId);
            frameId = null;
            lastTimestamp = null;
        }

        function canRender() {
            return resources && !contextLost && pageActive && !document.hidden && !document.fullscreenElement && !document.body.classList.contains('camera-cinema');
        }

        function fail(error) {
            stop();
            release();
            canvas.style.display = 'none';
            available = false;
            publish();
            console.warn('[sea-bg]', error.message);
        }

        function schedule() {
            if (frameId === null && canRender()) frameId = requestAnimationFrame(frame);
        }

        function frame(now) {
            frameId = null;
            if (!canRender()) { lastTimestamp = null; return; }
            // Время сцены, а не время загрузки страницы: пауза не дает скачка волн.
            var dt = lastTimestamp === null ? 0 : Math.min(Math.max((now - lastTimestamp) / 1000, 0), .05);
            lastTimestamp = now;
            time += dt;
            var follow = 1 - Math.exp(-dt * 3);
            mouseX += (targetX - mouseX) * follow;
            mouseY += (targetY - mouseY) * follow;
            try { render(); } catch (error) { fail(error); return; }
            if (animated) schedule();
        }

        function sync() {
            stop();
            if (!canRender()) { stop(); return; }
            schedule();
        }

        window.addEventListener('resize', function () { sizeDirty = true; sync(); }, { passive: true });
        window.addEventListener('pointermove', function (event) {
            if (!width || !height) return;
            targetX = Math.max(-1, Math.min(1, event.clientX / width * 2 - 1));
            targetY = Math.max(-1, Math.min(1, 1 - event.clientY / height * 2));
        }, { passive: true });
        document.addEventListener('visibilitychange', sync);
        document.addEventListener('fullscreenchange', sync);
        window.addEventListener('dno:camera-fullscreen', sync);
        motion.addEventListener?.('change', function () {
            if (preference === null) { animated = !motion.matches; publish(); sync(); }
        });
        window.addEventListener('pagehide', function () { pageActive = false; stop(); });
        window.addEventListener('pageshow', function () { pageActive = true; sync(); });
        canvas.addEventListener('webglcontextlost', function (event) {
            event.preventDefault();
            contextLost = true;
            stop();
            resources = null; // GPU освободил объекты потерянного контекста.
            canvas.style.display = 'none';
            available = false;
            publish();
        });
        canvas.addEventListener('webglcontextrestored', function () {
            contextLost = false;
            try { initialize(); available = true; publish(); sync(); } catch (error) { fail(error); }
        });

        try { initialize(); available = true; publish(); sync(); } catch (error) { fail(error); }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', main, { once: true });
    else main();
})();
