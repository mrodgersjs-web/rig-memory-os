/* ============================================================================
   RIG DESIGN SYSTEM — shared/rig-design-system.js
   Interactive components: sticky CTA, canvas background, accordion, scroll reveal.
   Link: <script src="./shared/rig-design-system.js" defer></script>
   ============================================================================ */
(function(){
  'use strict';

  /* 1. STICKY CTA — shows when hero scrolls out of view */
  window.RIGinitStickyCTA = function(ctaId, heroSelector){
    var cta=document.getElementById(ctaId),hero=document.querySelector(heroSelector||'.hero-full,.hero');
    if(!cta||!hero)return;
    var shown=false;
    new IntersectionObserver(function(e){
      e.forEach(function(en){
        if(!en.isIntersecting&&!shown){cta.classList.add('show');shown=true}
      });
    },{threshold:0}).observe(hero);
    var close=cta.querySelector('.sticky-cta__close,[onclick*="remove"]');
    if(close){
      close.onclick=function(){cta.classList.remove('show')};
    }
  };

  /* 2. CANVAS BACKGROUND — subtle gold particles */
  window.RIGinitCanvas = function(canvasId){
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
    var c=document.getElementById(canvasId);if(!c)return;
    var ctx=c.getContext('2d'),w,h,particles=[],N=35;
    function resize(){w=c.width=c.offsetWidth;h=c.height=c.offsetHeight}
    function init(){resize();particles=[];for(var i=0;i<N;i++)particles.push({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.12,vy:(Math.random()-.5)*.12,r:Math.random()*1.5+.5,o:Math.random()*.25+.05})}
    init();addEventListener('resize',init);
    var visible=true;
    new IntersectionObserver(function(e){visible=e[0].isIntersecting},{threshold:0}).observe(c);
    function draw(){
      if(!visible){requestAnimationFrame(draw);return}
      ctx.clearRect(0,0,w,h);
      for(var i=0;i<N;i++){
        var p=particles[i];p.x+=p.vx;p.y+=p.vy;
        if(p.x<0)p.x=w;if(p.x>w)p.x=0;if(p.y<0)p.y=h;if(p.y>h)p.y=0;
        ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fillStyle='rgba(200,169,110,'+p.o+')';ctx.fill();
      }
      requestAnimationFrame(draw);
    }
    draw();
  };

  /* 3. SCROLL REVEAL — fade in elements on scroll */
  window.RIGinitReveal = function(selector){
    var els=document.querySelectorAll(selector||'[data-reveal]');
    if(!els.length)return;
    var obs=new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if(en.isIntersecting){en.target.style.opacity='1';en.target.style.transform='translateY(0)';obs.unobserve(en.target)}
      });
    },{threshold:0.1,rootMargin:'0px 0px -50px 0px'});
    els.forEach(function(el){
      el.style.opacity='0';el.style.transform='translateY(20px)';el.style.transition='opacity .6s cubic-bezier(.16,1,.3,1),transform .6s cubic-bezier(.16,1,.3,1)';
      obs.observe(el);
    });
  };

  /* 4. AUTO-INIT on DOMContentLoaded */
  document.addEventListener('DOMContentLoaded',function(){
    // Auto-init sticky CTA if present
    var sticky=document.querySelector('.sticky-cta');
    if(sticky&&!sticky.dataset.manual)window.RIGinitStickyCTA(sticky.id||'sticky-cta');
    // Auto-init canvas if present
    var canvas=document.querySelector('[data-rig-canvas]');
    if(canvas)window.RIGinitCanvas(canvas.id||canvas.dataset.rigCanvas);
    // Auto-init reveal
    window.RIGinitReveal();
  });
})();
