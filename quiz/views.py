import json
import random
import urllib.request
import urllib.error
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .questions_data import QUESTIONS


class RateLimitError(Exception):
    pass


def home(request):
    if request.method == 'POST':
        mode = request.POST.get('mode', 'sequential')
        custom_key = request.POST.get('api_key', '').strip()
        custom_provider = request.POST.get('provider', '')

        if custom_key and custom_provider:
            request.session['api_key'] = custom_key
            request.session['provider'] = custom_provider
            request.session['use_default'] = False
        else:
            request.session['use_default'] = True
            request.session.pop('api_key', None)
            request.session.pop('provider', None)

        request.session['mode'] = mode
        return redirect('quiz_start')

    return render(request, 'quiz/home.html')


@csrf_exempt
def api_preflight(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    providers = []
    if settings.GEMINI_API_KEY:
        providers.append(('gemini', settings.GEMINI_API_KEY))
    if settings.GROQ_API_KEY:
        providers.append(('groq', settings.GROQ_API_KEY))

    if not providers:
        return JsonResponse({'available': False, 'error': 'Nema konfiguriranih API ključeva.'})

    for provider, api_key in providers:
        try:
            test_llm_call(provider, api_key)
            return JsonResponse({'available': True, 'provider': provider})
        except RateLimitError:
            continue
        except Exception:
            continue

    return JsonResponse({
        'available': False,
        'error': 'Svi ugrađeni API ključevi su dostigli limit. Unesite svoj ključ da nastavite.'
    })


def test_llm_call(provider, api_key):
    if provider == 'gemini':
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": "Odgovori samo: OK"}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 5}
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimitError()
            raise

    elif provider == 'groq':
        from openai import OpenAI, RateLimitError as OpenAIRateLimit
        try:
            client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
            client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": "Odgovori samo: OK"}],
                temperature=0,
                max_tokens=5,
            )
            return True
        except OpenAIRateLimit:
            raise RateLimitError()
        except Exception as e:
            error_str = str(e).lower()
            if '429' in error_str or ('rate' in error_str and 'limit' in error_str):
                raise RateLimitError()
            raise


def quiz_start(request):
    use_default = request.session.get('use_default', True)
    if not use_default and 'api_key' not in request.session:
        return redirect('home')

    mode = request.session.get('mode', 'sequential')
    indices = list(range(len(QUESTIONS)))
    if mode == 'random':
        random.shuffle(indices)

    request.session['question_indices'] = indices
    request.session['current_index'] = 0
    request.session['score'] = 0
    request.session['results'] = []
    return redirect('quiz_question')


def quiz_question(request):
    use_default = request.session.get('use_default', True)
    if not use_default and 'api_key' not in request.session:
        return redirect('home')

    indices = request.session.get('question_indices', [])
    current_index = request.session.get('current_index', 0)

    if current_index >= len(indices):
        return redirect('quiz_results')

    question_index = indices[current_index]
    question = QUESTIONS[question_index]
    total = len(indices)

    context = {
        'question': question,
        'current_num': current_index + 1,
        'total': total,
        'progress_percent': int((current_index / total) * 100),
    }
    return render(request, 'quiz/question.html', context)


@csrf_exempt
def check_answer(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
        user_answer = data.get('answer', '').strip()
        question_id = data.get('question_id')
        override_provider = data.get('provider')
        override_key = data.get('api_key')

        if not user_answer:
            return JsonResponse({'error': 'Odgovor je prazan.'}, status=400)

        question = next((q for q in QUESTIONS if q['id'] == question_id), None)
        if not question:
            return JsonResponse({'error': 'Pitanje nije pronađeno.'}, status=404)

        eval_kwargs = dict(
            question=question['question'],
            correct_answer=question['answer'],
            user_answer=user_answer,
            topic=question['topic']
        )

        if override_key and override_provider:
            try:
                result = evaluate_answer(
                    api_key=override_key,
                    provider=override_provider,
                    **eval_kwargs
                )
                request.session['api_key'] = override_key
                request.session['provider'] = override_provider
                request.session['use_default'] = False
                request.session.modified = True
            except RateLimitError:
                return JsonResponse({
                    'needs_manual_key': True,
                    'error': 'I ovaj ključ je dostigao limit. Pokušajte sa drugim ključem ili drugim servisom.'
                }, status=429)

        elif request.session.get('api_key') and not request.session.get('use_default', True):
            try:
                result = evaluate_answer(
                    api_key=request.session['api_key'],
                    provider=request.session.get('provider', 'gemini'),
                    **eval_kwargs
                )
            except RateLimitError:
                request.session.pop('api_key', None)
                request.session.pop('provider', None)
                request.session['use_default'] = True
                request.session.modified = True

                fallback = evaluate_with_fallback(**eval_kwargs)
                if fallback.get('needs_manual_key'):
                    return JsonResponse({
                        'needs_manual_key': True,
                        'error': 'Vaš ključ je dostigao limit, a ugrađeni ključevi su također nedostupni. Unesite novi ključ.'
                    }, status=429)
                result = fallback

        else:
            result = evaluate_with_fallback(**eval_kwargs)

        if result.get('needs_manual_key'):
            return JsonResponse({'needs_manual_key': True, 'error': result['error']}, status=429)

        results = request.session.get('results', [])
        results.append({
            'question_id': question_id,
            'question_short': question['question'][:80] + '...',
            'topic': question['topic'],
            'user_answer': user_answer,
            'correct_answer': question['answer'],
            'is_correct': result['is_correct'],
            'score': result['score'],
            'feedback': result['feedback'],
        })
        request.session['results'] = results
        request.session.modified = True

        if result['is_correct'] or result.get('score', 0) >= 75:
            request.session['score'] = request.session.get('score', 0) + 1

        return JsonResponse(result)

    except Exception as e:
        return JsonResponse({'error': f'Greška: {str(e)}'}, status=500)


def evaluate_with_fallback(question, correct_answer, user_answer, topic):
    providers = []

    if settings.GEMINI_API_KEY:
        providers.append(('gemini', settings.GEMINI_API_KEY))
    if settings.GROQ_API_KEY:
        providers.append(('groq', settings.GROQ_API_KEY))

    if not providers:
        return {
            'needs_manual_key': True,
            'error': 'Nema konfiguriranih API ključeva. Unesite svoj ključ.'
        }

    for provider, api_key in providers:
        try:
            return evaluate_answer(
                api_key=api_key,
                provider=provider,
                question=question,
                correct_answer=correct_answer,
                user_answer=user_answer,
                topic=topic
            )
        except RateLimitError:
            continue
        except Exception:
            continue

    return {
        'needs_manual_key': True,
        'error': 'Svi ugrađeni API ključevi su dostigli limit. Unesite svoj ključ da nastavite.'
    }


SYSTEM_PROMPT = """Ti si asistent i profesor građanskog prava koji ocjenjuje odgovore studenata.

Tvoj zadatak je da:
1. Usporediš studentov odgovor sa tačnim odgovorom
2. Provjeriš jesu li ključni pravni pojmovi i zaključci prisutni
3. Budeš fleksibilan — student ne mora koristiti iste riječi, ali mora imati ispravan pravni zaključak
4. Daš konstruktivnu povratnu informaciju na bosanskom/srpskom/hrvatskom jeziku

Odgovori ISKLJUČIVO u JSON formatu bez ikakvog teksta izvan JSON-a:
{
  "is_correct": true/false,
  "score": 0-100,
  "feedback": "Detaljno objašnjenje šta je dobro/loše u odgovoru",
  "missing_points": ["ključna tačka koja nedostaje 1", "..."],
  "correct_points": ["što je student dobro naveo 1", "..."]
}"""


def build_user_message(topic, question, correct_answer, user_answer):
    return f"""Tema: {topic}

PITANJE:
{question}

TAČAN ODGOVOR (referentni):
{correct_answer}

STUDENTOV ODGOVOR:
{user_answer}

Ocijeni studentov odgovor i vrati SAMO JSON."""


def parse_llm_json(content):
    content = content.strip()
    if '```' in content:
        parts = content.split('```')
        for part in parts:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('{'):
                content = part
                break
    start = content.find('{')
    end = content.rfind('}')
    if start != -1 and end != -1:
        content = content[start:end+1]
    return json.loads(content)


def evaluate_answer(api_key, provider, question, correct_answer, user_answer, topic):
    try:
        if provider == 'openai':
            result_data = call_openai_compatible(
                api_key=api_key,
                base_url="https://api.openai.com/v1",
                model="gpt-3.5-turbo",
                topic=topic, question=question,
                correct_answer=correct_answer, user_answer=user_answer
            )
        elif provider == 'groq':
            result_data = call_openai_compatible(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1",
                model="llama-3.3-70b-versatile",
                topic=topic, question=question,
                correct_answer=correct_answer, user_answer=user_answer
            )
        elif provider == 'gemini':
            result_data = call_gemini(
                api_key=api_key,
                topic=topic, question=question,
                correct_answer=correct_answer, user_answer=user_answer
            )
        else:
            raise ValueError(f"Nepoznat provider: {provider}")

        return {
            'is_correct': result_data.get('is_correct', False),
            'score': result_data.get('score', 0),
            'feedback': result_data.get('feedback', 'Nema povratne informacije.'),
            'missing_points': result_data.get('missing_points', []),
            'correct_points': result_data.get('correct_points', []),
            'correct_answer': correct_answer,
        }

    except RateLimitError:
        raise

    except json.JSONDecodeError:
        return {
            'is_correct': False, 'score': 0,
            'feedback': 'AI nije vratio ispravan format. Pokušajte ponovo.',
            'missing_points': [], 'correct_points': [], 'correct_answer': correct_answer,
        }


def call_openai_compatible(api_key, base_url, model, topic, question, correct_answer, user_answer):
    from openai import OpenAI, RateLimitError as OpenAIRateLimit
    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(topic, question, correct_answer, user_answer)}
            ],
            temperature=0.3,
            max_tokens=700,
        )
        return parse_llm_json(response.choices[0].message.content)
    except OpenAIRateLimit:
        raise RateLimitError("Rate limit reached")
    except Exception as e:
        error_str = str(e).lower()
        if 'rate' in error_str and 'limit' in error_str:
            raise RateLimitError("Rate limit reached")
        if '429' in error_str:
            raise RateLimitError("Rate limit reached")
        raise


def call_gemini(api_key, topic, question, correct_answer, user_answer):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": SYSTEM_PROMPT + "\n\n" + build_user_message(topic, question, correct_answer, user_answer)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_llm_json(text)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError("Gemini rate limit reached")
        raise


def next_question(request):
    if 'current_index' in request.session:
        request.session['current_index'] = request.session['current_index'] + 1
    return redirect('quiz_question')


def quiz_results(request):
    results = request.session.get('results', [])
    score = request.session.get('score', 0)
    total = len(request.session.get('question_indices', []))

    if total == 0:
        return redirect('home')

    percentage = int((score / total) * 100) if total > 0 else 0

    context = {
        'results': results,
        'score': score,
        'total': total,
        'percentage': percentage,
    }
    return render(request, 'quiz/results.html', context)


def restart(request):
    request.session.flush()
    return redirect('home')
