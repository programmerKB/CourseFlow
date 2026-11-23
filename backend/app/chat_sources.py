from urllib.parse import urlencode


def source(term_id=None, section_id=None, title='課程查詢', url=None):
    params = {}
    if term_id:
        params['term'] = str(term_id)
    if section_id:
        params['section'] = str(section_id)
    return {
        'title': title, 'url': url or ('/courses?' + urlencode(params) if params else '/courses'),
        **({'term_id': params['term']} if term_id else {}),
        **({'section_id': params['section']} if section_id else {}),
    }
