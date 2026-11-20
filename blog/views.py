"""📝 صفحات المدونة: /blog/ و /blog/<slug>/."""
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.safestring import mark_safe

from .markdown import render as md
from .models import Article


def _live():
    return Article.objects.filter(is_published=True, published_at__lte=timezone.now())


def index(request):
    page = Paginator(_live(), 12).get_page(request.GET.get('page'))
    return render(request, 'blog/index.html', {'page': page})


def article(request, slug):
    a = get_object_or_404(Article, slug=slug)
    user = getattr(request, 'user', None)
    if not a.is_live and not (user and user.is_authenticated and user.is_staff):
        raise Http404
    body, toc = md(a.body)
    body = mark_safe(body)   # markdown.render بيعمل escape لكل النص قبل التحويل
    related = list(_live().exclude(pk=a.pk).filter(sector=a.sector)[:3])
    service = None
    if a.service:
        from print_market.seo_data import SERVICES
        service = SERVICES.get(a.service)
    return render(request, 'blog/article.html', {
        'a': a, 'body': body, 'toc': toc, 'related': related, 'service': service,
        'seo_jsonld': _ld(request, a),
    })


def _ld(request, a):
    from erp_core.seo import SITE_NAME, _static_url, breadcrumbs_ld, jsonld, public_base
    url = public_base() + a.get_absolute_url()
    img = (a.cover.url if a.cover.url.startswith('http') else public_base() + a.cover.url) if a.cover \
        else _static_url('seo/og-printing.jpg')
    post = {'@context': 'https://schema.org', '@type': 'BlogPosting', 'headline': a.title,
            'description': a.description, 'image': img, 'url': url, 'mainEntityOfPage': url,
            'datePublished': a.published_at.isoformat(), 'dateModified': a.updated_at.isoformat(),
            'inLanguage': 'ar', 'author': {'@type': 'Organization', 'name': SITE_NAME, 'url': public_base()},
            'publisher': {'@type': 'Organization', 'name': SITE_NAME,
                          'logo': {'@type': 'ImageObject', 'url': _static_url('icon-512.png')}}}
    crumbs = breadcrumbs_ld([('Mouss Tec', '/'), ('المقالات', '/blog/'), (a.title, a.get_absolute_url())])
    return jsonld(post) + jsonld(crumbs)
