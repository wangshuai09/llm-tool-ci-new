import sys
import json

import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader
from peft import (
    get_peft_model,
    LoraConfig,
)

import evaluate
from datasets import load_dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
    OPTConfig,
    OPTForSequenceClassification,
    GPT2Config,
    GPT2ForSequenceClassification
)
from tqdm import tqdm


def main(model_name_or_path, dataset_path, task, device, evaluate_path):
    batch_size = 32
    num_epochs = 20
    peft_config = LoraConfig(task_type="SEQ_CLS", inference_mode=False, r=8, lora_alpha=16, lora_dropout=0.1)
    lr = 3e-4
    if any(k in model_name_or_path for k in ("gpt", "opt", "bloom")):
        padding_side = "left"
    else:
        padding_side = "right"
    if "opt-" in model_name_or_path:
        config = OPTConfig()

    elif "gpt2" in model_name_or_path:
        config = GPT2Config()
    else:
        config = None
    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path, padding_side=padding_side)
    if getattr(tokenizer, "pad_token_id") is None or tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    datasets = load_dataset(dataset_path, task)
    metric = evaluate.load(evaluate_path, task)

    def tokenize_function(examples):
        # max_length=None => use the model max length (it's actually the default)
        outputs = tokenizer(examples["sentence1"], examples["sentence2"], truncation=True, max_length=None)
        return outputs

    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        remove_columns=["idx", "sentence1", "sentence2"],
    )

    # We also rename the 'label' column to 'labels' which is the expected name for labels by the models of the
    # transformers library
    tokenized_datasets = tokenized_datasets.rename_column("label", "labels")

    def collate_fn(examples):
        return tokenizer.pad(examples, padding="longest", return_tensors="pt")

    # Instantiate dataloaders.
    train_dataloader = DataLoader(tokenized_datasets["train"], shuffle=True, collate_fn=collate_fn,
                                  batch_size=batch_size)
    eval_dataloader = DataLoader(
        tokenized_datasets["validation"], shuffle=False, collate_fn=collate_fn, batch_size=batch_size
    )
    if "opt-" in model_name_or_path:
        model = OPTForSequenceClassification(config).from_pretrained(model_name_or_path, return_dict=True)
        model.config.pad_token_id = model.config.eos_token_id
    elif "gpt2" in model_name_or_path:
        model = GPT2ForSequenceClassification(config).from_pretrained(model_name_or_path, return_dict=True)
        model.config.pad_token_id = model.config.eos_token_id
    else:
        model = AutoModelForSequenceClassification.from_pretrained(model_name_or_path, return_dict=True)
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()
    optimizer = AdamW(params=model.parameters(), lr=lr)

    # Instantiate scheduler
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=0.06 * (len(train_dataloader) * num_epochs),
        num_training_steps=(len(train_dataloader) * num_epochs),
    )

    model.to(device)
    for epoch in range(num_epochs):
        model.train()
        for step, batch in enumerate(tqdm(train_dataloader)):
            batch.to(device)
            outputs = model(**batch)
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

        model.eval()
        for step, batch in enumerate(tqdm(eval_dataloader)):
            batch.to(device)
            with torch.no_grad():
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            predictions, references = predictions, batch["labels"]
            metric.add_batch(
                predictions=predictions,
                references=references,
            )

        eval_metric = metric.compute()
        print(f"epoch {epoch}:", eval_metric)
        return eval_metric


if __name__ == '__main__':
    dataset_ = "/opt/nlp_data/glue/glue.py"
    evaluate_path = "/opt/projects/evaluate/metrics/glue/glue.py"
    task_ = "mrpc"
    device_ = "npu:0"
    args = sys.argv[1:]
    json_file = args[0]
    result_dict = {}
    with open(json_file, 'r') as fp:
        model_dict = json.load(fp)

        result_ = {}
        for key, model_ in model_dict.items():
            print("----------------" * 10)
            print(model_)
            try:
                acc = main(model_, dataset_, task_, device_, evaluate_path).get("accuracy") * 100
                if acc < 50:
                    print("%s accuracy=%s%%<60%%" % (model_, str(acc)))
                    result_[key] = False
                else:
                    result_[key] = True
            except Exception as e:
                print("%s get an error:" % model_)
                print(e)
                result_[key] = False

        with open("peft-models.json", "w") as fp:
            json.dump(result_, fp)