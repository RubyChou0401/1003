document.addEventListener('click',function(e){
  var c=e.target.closest('[data-confirm]');
  if(c&&!confirm(c.getAttribute('data-confirm'))){e.preventDefault();}
  var m=e.target.closest('.menu-btn');
  if(m){document.querySelector('.side').classList.toggle('open');}
});
document.addEventListener('click',function(e){var b=e.target.closest('[data-back]');if(b&&history.length>1){e.preventDefault();history.back();}});
