import json
import random
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .questions_data import QUESTIONS


def home(request):
    """Landing page — set API key and start quiz."""
    if request.method == 'POST':
        api_key = request.POST.get('api_key', '').strip()
        mode = request.POST.get('mode', 'sequential')
        provider = request.POST.get('provider', 'groq')
        if api_key:
            request.session['api_key'] = api_key
            request.session['mode'] = mode
            request.session['provider'] = provider
            return redirect('quiz_start')
    return render(request, 'quiz/home.html')


def quiz_start(request):
    """Initialize quiz session and redirect to first question."""
    if 'api_key' not in request.session:
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
    """Show current question."""
    if 'api_key' not in request.session:
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
    """AJAX endpoint: send user answer to LLM and get feedback."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    if 'api_key' not in request.session:
        return JsonResponse({'error': 'Nije podešen API ključ.'}, status=401)

    try:
        data = json.loads(request.body)
        user_answer = data.get('answer', '').strip()
        question_id = data.get('question_id')

        if not user_answer:
            return JsonResponse({'error': 'Odgovor je prazan.'}, status=400)

        question = next((q for q in QUESTIONS if q['id'] == question_id), None)
        if not question:
            return JsonResponse({'error': 'Pitanje nije pronađeno.'}, status=404)

        api_key = request.session['api_key']
        provider = request.session.get('provider', 'groq')

        result = evaluate_answer(
            api_key=api_key,
            provider=provider,
            question=question['question'],
            correct_answer=question['answer'],
            user_answer=user_answer,
            topic=question['topic']
        )

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
    """Robustly parse JSON from LLM response."""
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
    # Find first { ... }
    start = content.find('{')
    end = content.rfind('}')
    if start != -1 and end != -1:
        content = content[start:end+1]
    return json.loads(content)


def evaluate_answer(api_key, provider, question, correct_answer, user_answer, topic):
    """Dispatch to the correct LLM provider."""
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

    except json.JSONDecodeError:
        return {
            'is_correct': False, 'score': 0,
            'feedback': 'AI nije vratio ispravan format. Pokušajte ponovo.',
            'missing_points': [], 'correct_points': [], 'correct_answer': correct_answer,
        }


def call_openai_compatible(api_key, base_url, model, topic, question, correct_answer, user_answer):
    """Call any OpenAI-compatible API (OpenAI, Groq, etc.)."""
    from openai import OpenAI
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


def call_gemini(api_key, topic, question, correct_answer, user_answer):
    """Call Google Gemini API."""
    import urllib.request
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return parse_llm_json(text)


def next_question(request):
    """Advance to next question."""
    if 'current_index' in request.session:
        request.session['current_index'] = request.session['current_index'] + 1
    return redirect('quiz_question')


def quiz_results(request):
    """Show final results."""
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
    """Clear session and go back to home."""
    request.session.flush()
    return redirect('home')
