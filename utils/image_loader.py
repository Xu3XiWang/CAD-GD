import os
import torch
from torch.utils.data import Dataset, DataLoader
from groundingdino.util.base_api import preprocess_caption
from groundingdino.util.img_read import load_image
from utils.processor import DataProcessor
import io
def collate_fn(batch):
    
    images, labels, shapes, img_ids, density_maps = zip(*batch)

    # Get the max height and width among the images
    max_height = max([img.shape[1] for img in images])
    max_width = max([img.shape[2] for img in images])

    # Create tensors filled with zeros to store padded images
    padded_images = torch.zeros(len(images), 3, max_height, max_width)
    # Pad each image and add to the padded_images tensor
    for i, img in enumerate(images):
        padded_images[i, :, :img.shape[1], :img.shape[2]] = img

    # tuple to list
    labels = list(labels)
    shapes = list(shapes)
    img_ids = list(img_ids)
    # density_maps = list(density_maps)

    return padded_images, labels, shapes, img_ids, density_maps[0] # tensor (bs,3,h,w), list (), list ((w,h)), list ()

def get_loader(processor: DataProcessor, split, batch_size):
    
    split_set = Rec8KDataset(processor, split)
    
    shuffle = True if split == 'train' else False
    split_loader = DataLoader(split_set, batch_size=batch_size, shuffle=shuffle, collate_fn=collate_fn)
    

    return split_loader


class Rec8KDataset(Dataset):
    def __init__(self, processor: DataProcessor, split): 
        
        self.processor = processor
        self.split = split

        split_set_tuples = processor.get_img_ids_for_split(split) # list of (img_id, cap)
        self.density_dir = './datasets/rec-8k/density_maps'
        split_dict = {}
        for img_id, cap in split_set_tuples:
            if img_id in split_dict:
                split_dict[img_id].append(cap)
            else:
                split_dict[img_id] = [cap]

        self.img_ids = list(split_dict.keys())
        self.labels = [list(split_dict[img_id]) for img_id in self.img_ids] # list of list of caps
        # 2245 - 2250
        # self.img_ids = self.img_ids[:10]
        # self.labels = self.labels[:10]
        
        self.img_cap_tuples = []
        for i, (img_id, caps) in enumerate(zip(self.img_ids, self.labels)):
            img_cap_tuple = [(img_id, cap) for cap in caps] 
            self.img_cap_tuples.append(img_cap_tuple)
            for j, cap in enumerate(caps):
                text_prompt = processor.get_prompt_for_image((img_id, cap))[0]
                self.labels[i][j] = preprocess_caption(caption=text_prompt)
                
    def __len__(self):
        return len(self.img_ids)

    def __getitem__(self, idx):
        density_dir = os.path.join(self.density_dir, self.img_ids[idx][:-4])

        img_path = self.processor.get_image_path()
        img_file = os.path.join(img_path, self.img_ids[idx])

        label = self.labels[idx] # list of caps for same image

        image_source, image, density_maps = load_image(img_file, density_dir, label)
        h, w, _ = image_source.shape
        
        img_cap_tuple = self.img_cap_tuples[idx]  # list of tuples (img_id, cap) for same image

        return image, label, (h, w), img_cap_tuple, density_maps

