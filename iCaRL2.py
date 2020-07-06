import numpy as np
import time
import random
from copy import deepcopy

import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.nn import MSELoss

from sklearn.metrics import accuracy_score
from matplotlib import pyplot as plt

from IncrementalLearning import utils

class iCaRL2():
    def __init__(self,memory=2000,device='cuda',params=None,plot=False,decay_policy=False):
        self.memory = memory
        self.device = device
        self.params = params
        self.plot = plot
        self.decay_policy = decay_policy
        self.teachers = []

    def __NMEClassifier__(self,data,batch,exemplars,net,n_classes,mode='NME'):
      print(f'\n ### NME ###')
      means = dict.fromkeys(np.arange(n_classes))
      net.eval()

      batch_map = utils.fillClassMap(batch,n_classes)

      print('   # Computing means ')
      for key in range(n_classes):
        if key in range(n_classes-10,n_classes):
          items = batch_map[key]
        else:
          items = exemplars[key]
        
        loader = DataLoader(items, batch_size=512, shuffle=False, num_workers=4, drop_last=False)
        mean = torch.zeros((1,64),device=self.device)
        for images, _ in loader:
          with torch.no_grad():
            images = images.to(self.device)
            flipped_images = torch.flip(images,[3])
            images = torch.cat((images,flipped_images))
            
            outputs = net(images,features=True)
            
            for output in outputs:
              mean += output
        mean = mean / ( 2 * len(items) ) 
        means[key] = mean / mean.norm()

      loader = DataLoader(data, batch_size=512, shuffle=False, num_workers=4, drop_last=False)

      predictions, label_list = [], []
      print('   # NME Predicting ')
      for images, labels in loader:
        images = images.to(self.device)
        label_list += list(labels)
        with torch.no_grad():
          outputs = net(images,features=True)
          for output in outputs:
            prediction = None
            if mode == 'NME':
              min_dist = 99999
              for key in means:
                dist = torch.dist(means[key],output)
                if dist < min_dist:
                  min_dist = dist
                  prediction = key
            elif mode == 'Cosine':
              max_similarity = 0
              for key in means:
                cosine = torch.sum(means[key]*output)
                if cosine > max_similarity:
                  max_similarity = cosine
                  prediction = key
            predictions.append(prediction)
          
      accuracy = accuracy_score(label_list,predictions)
      print(f'   # NME Accuracy: {accuracy}')

      return accuracy, predictions, label_list

    def __FCClassifier__(self,data,net,n_classes):
      print(f'\n ### FC Layer ###')
      print('   # FC Layer Predicting ')
      net.eval()
      
      running_corrects = 0.0
      label_list, predictions = [], []
      with torch.no_grad():
        loader = DataLoader(data, batch_size=512, shuffle=False, num_workers=4, drop_last=False)
        for images, labels in loader:
          images = images.to(self.device)
          labels = labels.to(self.device)

          outputs = torch.sigmoid(net(images))
          # Get predictions
          _, preds = torch.max(outputs.data, 1)
          # Update Corrects
          running_corrects += torch.sum(preds == labels.data).data.item()
          
          for prediction,label in zip(preds,labels):
            predictions.append(np.array(prediction.cpu()))
            label_list.append(np.array(label.cpu()))

        # Calculate Accuracy
        accuracy = running_corrects / len(data)
      
      print(f'   # FC Layer Accuracy: {accuracy}')
      return accuracy, predictions, label_list

    def __stabilize__(self,exemplars,net,n_classes):
        print('\n ### Stabilize Network ###')
        EPOCHS = self.params['EPOCHS2']
        BATCH_SIZE = self.params['BATCH_SIZE']
        LR = self.params['LR2']
        MOMENTUM = self.params['MOMENTUM']
        WEIGHT_DECAY = self.params['WEIGHT_DECAY']
         
        # Define Loss
        criterion = MSELoss()

        exemplars = self.__formatExemplars__(exemplars)
        
        # Define Dataloader
        loader = DataLoader(exemplars, batch_size=BATCH_SIZE, shuffle=True, num_workers=4, drop_last=True)

        net = net.to(self.device)
        optimizer = torch.optim.SGD(net.parameters(), lr=LR, momentum=MOMENTUM, weight_decay=WEIGHT_DECAY)
        milestones = set([ int(EPOCHS/3), int(2*EPOCHS/3) ])
        
        for epoch in range(EPOCHS):
         
          # LR step down policy
          if epoch in milestones:
            for g in optimizer.param_groups:
              g['lr'] = g['lr']/5
   
          # Set module in training mode
          net.train() 

          running_loss = 0.0
          for images, labels in loader:
            images = images.to(self.device)
            images = torch.stack([ utils.augmentation(image) for image in images ])
            
            # Zero-ing the gradients
            optimizer.zero_grad()
            # Forward pass to the network
            outputs = torch.sigmoid(net(images))
            # Get One Hot Encoding for the labels
            labels = utils.getOneHot(labels,n_classes)
            labels = labels.to(self.device)

            tot_loss = criterion(outputs,labels)   

            # Update Running Loss         
            running_loss += tot_loss.item() * images.size(0)

            tot_loss.backward() 
            optimizer.step() 

          # Train loss of current epoch
          train_loss = running_loss / len(exemplars)
          print('\r   # Epoch: {}/{}, LR = {},  Train loss = {}'.format(epoch+1, EPOCHS, optimizer.param_groups[0]['lr'], round(train_loss,5)),end='')
        print()

        return net
 
    def __updateRepresentation__(self,data,exemplars,net,n_classes,fineTune=False):
        print('\n ### Update Representation ###')
        EPOCHS = self.params['EPOCHS']
        BATCH_SIZE = self.params['BATCH_SIZE']
        LR = self.params['LR']
        MOMENTUM = self.params['MOMENTUM']
        WEIGHT_DECAY = self.params['WEIGHT_DECAY']
        lambda_ = self.params['lambda']
        
        if self.decay_policy:
          step = int(n_classes/10) - 1
          WEIGHT_DECAY = np.linspace(WEIGHT_DECAY,WEIGHT_DECAY/10,10)[step]
          lambda_ += 0.1 * ( step - 1 )

        # Define Loss
        criterion = MSELoss()

        if len(exemplars) != 0:
          data = data + self.__formatExemplars__(exemplars)
        
        # Define Dataloader
        loader = DataLoader(data, batch_size=BATCH_SIZE, shuffle=True, num_workers=4, drop_last=True)

        if n_classes != 10:
          # Save network for distillation
          old_net = deepcopy(net)
          old_net.eval()
          self.teachers.append(old_net)
          # Update network's last layer
          net = utils.updateNet(net,n_classes)
        
        net = net.to(self.device)
        optimizer = torch.optim.SGD(net.parameters(), lr=LR, momentum=MOMENTUM, weight_decay=WEIGHT_DECAY)
        
        for epoch in range(EPOCHS):
         
          # LR step down policy
          if epoch == 48 or epoch == 62:
            for g in optimizer.param_groups:
              g['lr'] = g['lr']/5
   
          # Set module in training mode
          net.train() 

          running_loss = 0.0
          for images, labels in loader:
            images = images.to(self.device)
            images = torch.stack([ utils.augmentation(image) for image in images ])
            
            # Zero-ing the gradients
            optimizer.zero_grad()
            # Forward pass to the network
            outputs = torch.sigmoid(net(images))
            # Get One Hot Encoding for the labels
            labels = utils.getOneHot(labels,n_classes)
            labels = labels.to(self.device)

            # Compute Losses
            if n_classes == 10 or fineTune:
                tot_loss = criterion(outputs,labels)
            else:
                with torch.no_grad():
                  old_outputs = self.__getOldOutputs__(n_classes,images)
                class_loss = criterion(outputs,labels)
                distill_loss = criterion(torch.pow(outputs[:,:n_classes-10],1/2),torch.pow(old_outputs,1/2))
                tot_loss = class_loss + distill_loss #* lambda_
                
            # Update Running Loss         
            running_loss += tot_loss.item() * images.size(0)

            tot_loss.backward() 
            optimizer.step() 

          # Train loss of current epoch
          train_loss = running_loss / len(data)
          print('\r   # Epoch: {}/{}, LR = {},  Train loss = {}'.format(epoch+1, EPOCHS, optimizer.param_groups[0]['lr'], round(train_loss,5)),end='')
        print()

        return net
    
    def __getOldOutputs__(self,n_classes,images):
      with torch.no_grad():
        for i in range(int(n_classes / 10)-1):
          if i == 0:
            outputs = torch.sigmoid(self.teachers[i](images))
          else:
            current_outputs = torch.sigmoid(self.teachers[i](images))[:,i*10:i*10+10]
            outputs = torch.cat((outputs,current_outputs),1)
      return outputs

    def __randomExemplarSet__(self,data,n_classes):
      print('\n ### Construct Random Exemplar Set ###')
      if n_classes != 10:
        m = int(self.memory/(n_classes-10))
      else:
        m = int(self.memory/(n_classes))
      print(f'   # Exemplars per class: {m}')

      # Initialize lists of images and exemplars for each class
      class_map = utils.fillClassMap(data,n_classes)
      exemplars = dict.fromkeys(np.arange(n_classes-10,n_classes))
      for label in exemplars:
        exemplars[label] = []

      for label in class_map:
        indexes = random.sample(range(len(class_map[label])),m)   
        for idx in indexes:
            exemplars[label].append(class_map[label][idx])

      return exemplars
    
    #herding
    def __constructExemplarSet__(self,data,n_classes,net):
        print('\n ### Construct Exemplar Set ###')
        if n_classes != 10:
          m = int(self.memory/(n_classes-10))
        else:
          m = int(self.memory/(n_classes))
        print(f'   # Exemplars per class: {m}')

        # Initialize lists of images and exemplars for each class
        class_map = utils.fillClassMap(data,n_classes)
        exemplars = dict.fromkeys(np.arange(n_classes-10,n_classes))
        for label in exemplars:
          exemplars[label] = []
        
        # Get and save net outputs for each class
        net.eval()
        for label in class_map:
          print(f'\r   # Class: {label+1}',end='')
          class_outputs = []
          mean = 0
          
          # Compute class means
          with torch.no_grad():
            loader = DataLoader(class_map[label], batch_size=512, shuffle=False, num_workers=4, drop_last=False)
            for images, _ in loader:
                images = images.to(self.device)
                outputs = net(images,features=True)
                for output in outputs:
                    class_outputs.append(output)
                    mean += output
            mean /= len(class_map[label])
          
            w_t = mean
            for i in range(m):
              maximum = -99999
              ind_max = None
              for idx,tensor in enumerate(class_outputs):
                dot = w_t.dot(tensor)

                if dot > maximum:
                  maximum = dot
                  ind_max = idx

              w_t = w_t+mean-class_outputs[ind_max]    
              class_outputs.pop(ind_max)
       
              exemplars[label].append(class_map[label][ind_max])
              class_map[label].pop(ind_max)
        print()

        return exemplars

    def __reduceExemplarSet__(self,exemplars,n_classes):
      print('\n ### Reduce Exemplar Set ###')
      m = int(self.memory/n_classes)
      print(f'   # Exemplars per class: {m}')
      for key in exemplars:
        exemplars[key] = exemplars[key][:m]
      
      return exemplars
    
    # dict to list
    def __formatExemplars__(self,exemplars):
      new_exemplars = []
      for key in exemplars:
        for item in exemplars[key]:
          new_exemplars.append([item[0],item[1]])

      return new_exemplars
      
    # Run ICaRL
    def run(self,train_batches,test_batches,net,herding=True,classifier='NME',NME_mode='NME'):
      t0 = time.time()
      exemplars = {}
      accuracy_per_batch = []
      for idx, batch in enumerate(train_batches):
        print(f'\n##### BATCH {idx+1} #####')
        n_classes = (idx+1)*10

        # Update Representation
        net = self.__updateRepresentation__(batch,exemplars,net,n_classes)
        utils.printTime(t0)
        
        if herding:
          new_exemplars = self.__constructExemplarSet__(batch,n_classes,net)
        else:
          new_exemplars = self.__randomExemplarSet__(batch,n_classes)
        exemplars.update(new_exemplars)
        utils.printTime(t0)
        
        if idx != 0:
          self.__FCClassifier__(test_batches[idx],net,n_classes)
          utils.printTime(t0)
          self.__NMEClassifier__(test_batches[idx],batch,exemplars,net,n_classes,NME_mode)
          utils.printTime(t0)
          net = self.__stabilize__(exemplars,net,n_classes)
          utils.printTime(t0)
        
        # Classifier
        if classifier == 'NME':
          self.__FCClassifier__(test_batches[idx],net,n_classes)
          utils.printTime(t0)
          accuracy, predictions, labels = self.__NMEClassifier__(test_batches[idx],batch,exemplars,net,n_classes,NME_mode)
        elif classifier == 'FC':
          accuracy, predictions, labels = self.__FCClassifier__(test_batches[idx],net,n_classes)
        else:
          accuracy, predictions, labels = self.__SKLClassifier__(test_batches[idx],batch,exemplars,net,n_classes,classifier)
        accuracy_per_batch.append(accuracy)
        utils.printTime(t0)
        
        if self.plot:
          utils.confusionMatrix(labels,predictions,idx)

        # Exemplars managing
        exemplars = self.__reduceExemplarSet__(exemplars,n_classes)
        utils.printTime(t0)

      return accuracy_per_batch
    
    # Run LwF
    def runLwF(self,train_batches,test_batches,net,fineTune=False):
      t0 = time.time()
      accuracy_per_batch = []
      for idx, batch in enumerate(train_batches):
        print(f'\n##### BATCH {idx+1} #####')
        n_classes = (idx+1)*10
        net = self.__updateRepresentation__(batch,{},net,n_classes,fineTune)
        utils.printTime(t0)
        
        accuracy, predictions, labels = self.__FCClassifier__(test_batches[idx],net,n_classes)
        accuracy_per_batch.append(accuracy)
        utils.printTime(t0)
        
        if self.plot:
            utils.confusionMatrix(labels,predictions,idx)

      return accuracy_per_batch
