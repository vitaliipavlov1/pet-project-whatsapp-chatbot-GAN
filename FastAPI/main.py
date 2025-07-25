from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import requests
from . import models
from .database import engine
from openai import OpenAI
import os
import torch
from dotenv import load_dotenv
from unet_generator_chatbot.unet_generator import UNetGenerator
from patch_discriminator_chatbot import PatchDiscriminator
load_dotenv()


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

model_gen = UNetGenerator().eval().to(device)
model_dis = PatchDiscriminator().eval().to(device)

if not os.path.exists('model_gen.tar'):
    raise FileNotFoundError("File model_gen.tar no encontrado")

st_gen = torch.load('model_gen.tar', weights_only=True)
model_gen.load_state_dict(st_gen)

if not os.path.exists('model_dis.tar'):
    raise FileNotFoundError("File model_dis.tar no encontrado")

st_dis = torch.load('model_dis.tar', weights_only=True)
model_dis.load_state_dict(st_dis)

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

# instrucción del sistema.
SYSTEM_PROMPT = '''Eres un chatbot en español en WhatsApp para una empresa privada de fabricación de prendas de vestir. Todas tus respuestas deben estar en el idioma del usuario o en el idioma especificado por el usuario. Responde únicamente a mensajes relacionados con la fabricacion de las prendas, ropa y textil.
No se permite ningún lenguaje grosero o ilegal, ni del usuario ni del chatbot.
Para mensajes no relacionados con la informacion indicada anteriormente, mostrar: Estimado usuario, este es el chatbot unicamente de la tematica de prendas, ropa y textil.
Para mensajes con lenguaje grosero o ilegal, mostrar: De acuerdo con la política de chatbot, cualquier lenguaje grosero o ilegal está totalmente prohibido.'''


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return JSONResponse(content=int(params.get("hub.challenge")))
    return JSONResponse(status_code=403, content={"error": "Verification failed"})

@app.post("/webhook")
async def receive_whatsapp_message(request: Request):
    body = await request.json()
    print("Recibido:", body)

    try:
        messages = body["entry"][0]["changes"][0]["value"].get("messages")
        if messages:
            msg = messages[0]
            from_number = msg["from"]
            msg_type = msg['type']

            text = msg['text']['body'].strip().lower()

            # Verificacion y, en el caso de no estar registrado, registro.
            if text.startswith("sign up") and len(text) == 7:
                existing_user = db.query(User).filter_by(phone_number=from_number).first()
                if existing_user:
                    send_whatsapp_message(from_number, text='El usuario ya esta registrado.')
                else:
                    new_user = User(phone_number=from_number)
                    db.add(new_user)
                    db.commit()
                    send_whatsapp_message(from_number, text=f"Registro esta completado con exito!")
                return {"status": "registered"}

             # Verificación de usuario.
            user = db.query(User).filter(User.phone_number == from_number).first()
            if not user:
                send_whatsapp_message(from_number, text="El usuario no esta registrado. Para registrarse, por favor, introduce: Sign Up")
                return {"status": "unauthorized"}
            if msg_type == "text" and user and text != 'sign up':
                text = msg["text"]["body"]
                reply_text = generate_chatgpt_reply(text)
                send_whatsapp_message(from_number, text=reply_text)

            elif msg_type == "image" and user:

                media_id = msg["image"]["id"]

                media_url = get_image_url(media_id)

                image_bytes = download_image(media_url)

                real_img = image_bytes_to_tensor(image_bytes)

                img_gen = model_gen(real_img)
                dis_out = model_dis(img_gen)

                heatmap_gen = create_heatmap_image(img_gen)
                heatmap_dis = create_heatmap_image(dis_out)

                media_id_gen = upload_image_to_whatsapp(heatmap_gen)
                media_id_dis = upload_image_to_whatsapp(heatmap_dis)

                # Añadiendo la imagen real y los heatmaps generados en la base de datos.
                new_real_image = Images(phone_number=from_number, real_image=image_bytes)
                db.add(new_real_image)
                db.commit()
                new_heatmap_gen_image = Images(phone_number=from_number, heatmap_gen=heatmap_gen)
                db.add(new_heatmap_gen_image)
                db.commit()
                new_heatmap_dis_image = Images(phone_number=from_number, heatmap_gen=heatmap_dis)
                db.add(new_heatmap_dis_image)
                db.commit()

                send_whatsapp_message(from_number, media_id=media_id_gen, caption='gen_generated')
                send_whatsapp_message(from_number, media_id=media_id_dis, caption='dis_generated')

            else:
                reply_text = f"tipo de mensaje '{msg_type}' aún no compatible."
                send_whatsapp_message(from_number, reply_text)

    except Exception as e:
        print("Error:", e)

    return {"status": "ok"}

def generate_chatgpt_reply(user_message: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print('Error al acceder a la IA', e)
        return "Error al acceder a la IA. Inténtalo más tarde."


def send_whatsapp_message(to_number: str, text: Optional[str], media_id: Optional[str] = None, caption: Optional[str] = None):

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    if text:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text}
        }
    elif media_id:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "image",
            "image": {
            "id": media_id,
            "caption": caption
        }
    }


    response = requests.post(url, json=payload, headers=headers)
    print("Respuesta de WhatsApp API:", response.json())




def get_image_url(media_id: str) -> str:
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()["url"]


def download_image(media_url: str) -> bytes:
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get(media_url, headers=headers)
    response.raise_for_status()
    return response.content   # bytes


def image_bytes_to_tensor(image_bytes: bytes) -> torch.Tensor:

    image = Image.open(BytesIO(image_bytes)).convert("RGB")    # Abriendo una imagen desde bytes

    # Transformaciones: redimensionar, normalizar y convertir en tensor
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),               # Convierte a [C x H x W], valores de 0 a 1
        transforms.Normalize(                # Normalización
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225]
        )
    ])

    tensor = transform(image)  # [3, 224, 224]
    return tensor.unsqueeze(0) # [1, 3, 224, 224] — batch de una imagen


def create_heatmap_image(tensor: torch.Tensor, title: str = "Heatmap anomaly detection") -> bytes:

    tensor = tensor.detach().cpu().squeeze().numpy()

    # Creacion de heatmap
    fig, ax = plt.subplots()
    heatmap = ax.imshow(tensor, cmap='viridis')
    ax.set_title(title)
    plt.colorbar(heatmap)

    # convertiento imagen heatmap en bytes
    buf = BytesIO()
    plt.savefig(buf, format='JPEG')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def upload_image_to_whatsapp(image_bytes: bytes) -> str:
    upload_url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/media"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    files = {
        'file': ('image.jpg', image_bytes, 'image/jpeg')
    }
    data = {
        'messaging_product': 'whatsapp'
    }
    response = requests.post(upload_url, headers=headers, files=files, data=data)
    response.raise_for_status()
    media_id = response.json()['id']
    return media_id

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
