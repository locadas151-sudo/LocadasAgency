(function(){
  var forms=document.querySelectorAll('form[data-netlify]');
  Array.prototype.forEach.call(forms,function(f){
    f.addEventListener('submit',function(e){
      e.preventDefault();
      var btn=f.querySelector('button[type=submit]');var txt=btn?btn.innerHTML:'';
      if(btn){btn.disabled=true;btn.textContent='Wird gesendet …';}
      var data=new FormData(f);
      var body=new URLSearchParams(data).toString();
      var done=function(){window.location.href=f.getAttribute('action')||'danke.html';};
      var fallback=function(){
        var lines=[];
        data.forEach(function(v,k){if(k==='form-name'||k==='website'||k==='firma'||!v)return;lines.push(k+': '+v);});
        var subject=(f.getAttribute('name')==='karriere'?'Jobs: ':'Erstgespräch: ')+(data.get('name')||'');
        if(btn){btn.disabled=false;btn.innerHTML=txt;}
        var n=f.querySelector('.form-note');
        if(!n){n=document.createElement('p');n.className='form-note';f.appendChild(n);}
        n.innerHTML='Dein E-Mail-Programm öffnet sich, einmal auf Senden drücken. Falls nicht: <a href="mailto:info@locadas.de">info@locadas.de</a>';
        window.location.href='mailto:info@locadas.de?subject='+encodeURIComponent(subject)+'&body='+encodeURIComponent(lines.join('\n'));
      };
      fetch(window.location.pathname,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
        .then(function(r){if(r.ok)done();else fallback();})
        .catch(fallback);
    });
  });
})();
