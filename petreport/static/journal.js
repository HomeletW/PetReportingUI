'use strict';
const openMoment = (id) => {
  const dialog = document.getElementById(id);
  if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
};
document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-open-dialog]');
  if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
    event.preventDefault();
    openMoment(link.dataset.openDialog);
  }
});
document.querySelectorAll('.moment-dialog').forEach((dialog) => {
  dialog.addEventListener('click', (event) => {
    const box = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.querySelectorAll('video').forEach((video) => video.pause()));
});
if (location.hash.startsWith('#moment-')) openMoment(location.hash.slice(1));
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#moment-')) openMoment(location.hash.slice(1)); });

document.querySelectorAll('[data-gallery-step]').forEach(button => {
  button.addEventListener('click', () => {
    const strip = button.closest('.photo-gallery').querySelector('.photos-swipe');
    const index = Math.round(strip.scrollLeft / strip.clientWidth);
    strip.scrollTo({left: (index + Number(button.dataset.galleryStep)) * strip.clientWidth,
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  });
});
